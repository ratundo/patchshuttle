"""Safety contracts for runtime ``__pycache__`` cleanup."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle.planner import Plan, plan_job
from patchshuttle.runtime_cache import (
    RuntimeCacheError,
    RuntimeCacheLedger,
    capture_runtime_cache_ledger,
    cleanup_runtime_caches,
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


def python_plan(workspace: Workspace) -> Plan:
    return plan_job(
        Job(
            protocol=1,
            project_id=PROJECT_ID,
            id="PATCH-RUNTIME-CACHE",
            kind="patch",
            actions=[
                {
                    "create_file": {
                        "path": "src/pkg/module.py",
                        "content": "VALUE = 1\n",
                    }
                }
            ],
            checks=[{"import_check": {"modules": ["json"]}}],
        ),
        workspace,
    )


def test_capture_preserves_every_preexisting_cache_entry(
    workspace: Workspace,
) -> None:
    plan = python_plan(workspace)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.cpython.pyc").write_bytes(b"baseline")
    (cache / "nested").mkdir()
    blocked = workspace.root / "src/__pycache__"
    blocked.write_text("pre-existing non-directory\n", encoding="utf-8")

    ledger = capture_runtime_cache_ledger(plan)
    cleanup = cleanup_runtime_caches(plan, ledger)

    assert ledger.roots == (
        PurePosixPath("."),
        PurePosixPath("src"),
        PurePosixPath("src/pkg"),
    )
    assert ledger.directories == frozenset({PurePosixPath("src/pkg/__pycache__")})
    assert ledger.non_directories == frozenset({PurePosixPath("src/__pycache__")})
    assert ledger.entries == frozenset(
        {
            PurePosixPath("src/pkg/__pycache__/module.cpython.pyc"),
            PurePosixPath("src/pkg/__pycache__/nested"),
        }
    )
    assert cleanup.success is True
    assert cleanup.removed_files == ()
    assert cleanup.removed_directories == ()
    assert (cache / "module.cpython.pyc").read_bytes() == b"baseline"
    assert (cache / "nested").is_dir()
    assert blocked.read_text("utf-8") == "pre-existing non-directory\n"


def test_cleanup_removes_only_new_pyc_files_and_empty_cache_directories(
    workspace: Workspace,
) -> None:
    plan = python_plan(workspace)
    ledger = capture_runtime_cache_ledger(plan)
    caches = (
        workspace.root / "__pycache__",
        workspace.root / "src/__pycache__",
        workspace.root / "src/pkg/__pycache__",
    )
    for index, cache in enumerate(caches):
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"generated-{index}.pyc").write_bytes(b"generated")

    cleanup = cleanup_runtime_caches(plan, ledger)

    assert cleanup.success is True
    assert len(cleanup.removed_files) == 3
    assert cleanup.removed_directories == (
        PurePosixPath("src/pkg/__pycache__"),
        PurePosixPath("src/__pycache__"),
        PurePosixPath("__pycache__"),
    )
    assert all(not cache.exists() for cache in caches)


def test_cleanup_refuses_new_foreign_entries_and_non_directory_cache(
    workspace: Workspace,
) -> None:
    plan = python_plan(workspace)
    ledger = capture_runtime_cache_ledger(plan)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    foreign = cache / "do-not-delete.txt"
    foreign.write_text("owner data\n", encoding="utf-8")
    nested = cache / "nested"
    nested.mkdir()

    cleanup = cleanup_runtime_caches(plan, ledger)

    assert cleanup.success is False
    assert cleanup.unresolved == (
        PurePosixPath("src/pkg/__pycache__/do-not-delete.txt"),
        PurePosixPath("src/pkg/__pycache__/nested"),
        PurePosixPath("src/pkg/__pycache__"),
    )
    assert foreign.exists()

    foreign.unlink()
    nested.rmdir()
    cache.rmdir()
    cache.write_text("not a directory\n", encoding="utf-8")
    second = cleanup_runtime_caches(plan, ledger)
    assert second.unresolved == (PurePosixPath("src/pkg/__pycache__"),)


def test_cleanup_rejects_a_ledger_for_another_scope(workspace: Workspace) -> None:
    plan = python_plan(workspace)
    forged = RuntimeCacheLedger(
        roots=(PurePosixPath("other"),),
        directories=frozenset(),
        non_directories=frozenset(),
        entries=frozenset(),
    )

    with pytest.raises(ValueError, match="scope"):
        cleanup_runtime_caches(plan, forged)


def test_capture_enforces_the_workspace_inventory_entry_limit(
    workspace: Workspace,
) -> None:
    plan = python_plan(workspace)
    execution = workspace.config.execution.model_copy(
        update={"max_inventory_entries": 1}
    )
    limited = replace(
        workspace,
        config=workspace.config.model_copy(update={"execution": execution}),
    )
    plan = replace(plan, workspace=limited)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    (cache / "one.pyc").write_bytes(b"one")
    (cache / "two.pyc").write_bytes(b"two")

    with pytest.raises(RuntimeCacheError) as caught:
        capture_runtime_cache_ledger(plan)

    assert caught.value.path in {
        PurePosixPath("src/pkg/__pycache__/one.pyc"),
        PurePosixPath("src/pkg/__pycache__/two.pyc"),
    }


@pytest.mark.parametrize("failure", ("metadata", "directory", "entry"))
def test_capture_maps_filesystem_inspection_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    plan = python_plan(workspace)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    child = cache / "module.pyc"
    child.write_bytes(b"cache")
    original_lstat = Path.lstat
    original_iterdir = Path.iterdir

    def failed_lstat(self: Path):
        if failure == "metadata" and self == cache:
            raise OSError("metadata failed")
        if failure == "entry" and self == child:
            raise OSError("entry failed")
        return original_lstat(self)

    def failed_iterdir(self: Path):
        if failure == "directory" and self == cache:
            raise OSError("directory failed")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "lstat", failed_lstat)
    monkeypatch.setattr(Path, "iterdir", failed_iterdir)

    with pytest.raises(RuntimeCacheError) as caught:
        capture_runtime_cache_ledger(plan)

    expected = (
        PurePosixPath("src/pkg/__pycache__/module.pyc")
        if failure == "entry"
        else PurePosixPath("src/pkg/__pycache__")
    )
    assert caught.value.path == expected


def test_cleanup_reports_inspection_and_removal_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = python_plan(workspace)
    ledger = capture_runtime_cache_ledger(plan)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    child = cache / "module.pyc"
    child.write_bytes(b"cache")
    original_unlink = Path.unlink

    def failed_unlink(self: Path, *args, **kwargs):
        if self == child:
            raise OSError("unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_unlink)

    cleanup = cleanup_runtime_caches(plan, ledger)

    assert cleanup.success is False
    assert cleanup.unresolved == (
        PurePosixPath("src/pkg/__pycache__/module.pyc"),
        PurePosixPath("src/pkg/__pycache__"),
    )
    assert child.exists()


@pytest.mark.parametrize("failure", ("metadata", "directory", "entry"))
def test_cleanup_maps_filesystem_failures_to_unresolved_paths(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    plan = python_plan(workspace)
    ledger = capture_runtime_cache_ledger(plan)
    cache = workspace.root / "src/pkg/__pycache__"
    cache.mkdir(parents=True)
    child = cache / "module.pyc"
    child.write_bytes(b"cache")
    original_lstat = Path.lstat
    original_iterdir = Path.iterdir

    def failed_lstat(self: Path):
        if failure == "metadata" and self == cache:
            raise OSError("metadata failed")
        if failure == "entry" and self == child:
            raise OSError("entry failed")
        return original_lstat(self)

    def failed_iterdir(self: Path):
        if failure == "directory" and self == cache:
            raise OSError("directory failed")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "lstat", failed_lstat)
    monkeypatch.setattr(Path, "iterdir", failed_iterdir)

    cleanup = cleanup_runtime_caches(plan, ledger)

    expected = (
        PurePosixPath("src/pkg/__pycache__/module.pyc")
        if failure == "entry"
        else PurePosixPath("src/pkg/__pycache__")
    )
    assert cleanup.success is False
    assert expected in cleanup.unresolved


def test_cleanup_detects_missing_or_type_changed_preexisting_cache_paths(
    workspace: Workspace,
) -> None:
    plan = python_plan(workspace)
    root_cache = workspace.root / "__pycache__"
    source_cache = workspace.root / "src/__pycache__"
    package_cache = workspace.root / "src/pkg/__pycache__"
    root_cache.mkdir()
    source_cache.parent.mkdir()
    source_cache.write_text("baseline file\n", encoding="utf-8")
    package_cache.mkdir(parents=True)
    ledger = capture_runtime_cache_ledger(plan)

    root_cache.rmdir()
    source_cache.unlink()
    source_cache.mkdir()
    package_cache.rmdir()
    package_cache.write_text("replacement file\n", encoding="utf-8")

    cleanup = cleanup_runtime_caches(plan, ledger)

    assert cleanup.unresolved == (
        PurePosixPath("src/pkg/__pycache__"),
        PurePosixPath("src/__pycache__"),
        PurePosixPath("__pycache__"),
    )

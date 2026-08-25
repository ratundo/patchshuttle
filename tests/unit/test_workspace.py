"""Contract tests for workspace discovery and safe initialization."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import patchshuttle.workspace as workspace_module
from patchshuttle import Job, load_job
from patchshuttle.config import ProjectOrigin
from patchshuttle.errors import WorkspaceError, WorkspaceErrorCode
from patchshuttle.models import AUDIT_ACTION_NAMES, CHANGE_ACTION_NAMES
from patchshuttle.workspace import (
    WorkspaceInitStatus,
    discover_workspace,
    find_child_workspaces,
    init_workspace,
    load_workspace,
)

PROJECT_ID = "PSH-8F41C2A73D905E61"
EXPECTED_CREATED_PATHS = {
    Path("patches"),
    Path("patches/inbox"),
    Path("patches/applied"),
    Path("patches/failed"),
    Path("patches/logs"),
    Path("patches/backups"),
    Path("patches/state"),
    Path("patches/examples"),
    Path("patches/patchshuttle.toml"),
    Path("patches/AI_GUIDE.md"),
    Path("patches/PATCHSHUTTLE_PROTOCOL.md"),
    Path("patches/patchshuttle.schema.json"),
    Path("patches/state/registry.json"),
    Path("patches/state/warning-baseline.json"),
    Path("patches/state/run.lock"),
    Path("patches/examples/AUDIT-EXAMPLE.psh.yaml"),
    Path("patches/examples/PATCH-EXAMPLE.psh.yaml"),
}
CHECK_NAMES = {
    "compileall",
    "pytest",
    "unittest",
    "django_check",
    "django_migrations_check",
    "django_test",
    "import_check",
    "profile",
}


@pytest.fixture
def fixed_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)


def test_existing_project_initialization_creates_the_complete_scaffold(
    tmp_path: Path, fixed_project_id: None
) -> None:
    user_file = tmp_path / "pyproject.toml"
    user_file.write_text("[project]\nname = 'example'\n", encoding="utf-8")

    result = init_workspace(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALIZED
    assert set(result.created_paths) == EXPECTED_CREATED_PATHS
    assert result.workspace.root == tmp_path.resolve()
    assert result.workspace.project_id == PROJECT_ID
    assert result.workspace.origin is ProjectOrigin.EXISTING
    assert result.workspace.patches_dir == tmp_path / "patches"
    assert result.workspace.config_path == tmp_path / "patches/patchshuttle.toml"
    result.workspace.require_project_id(PROJECT_ID)
    assert user_file.read_text(encoding="utf-8") == "[project]\nname = 'example'\n"

    registry = json.loads(
        (tmp_path / "patches/state/registry.json").read_text(encoding="utf-8")
    )
    assert registry == {"jobs": {}, "project_id": PROJECT_ID}
    assert (tmp_path / "patches/state/run.lock").read_bytes() == b""

    schema = json.loads(
        (tmp_path / "patches/patchshuttle.schema.json").read_text(encoding="utf-8")
    )
    assert schema == Job.model_json_schema()

    for name in ("AUDIT-EXAMPLE", "PATCH-EXAMPLE"):
        job = load_job(tmp_path / f"patches/examples/{name}.psh.yaml")
        assert job.project_id == PROJECT_ID

    ai_guide = (tmp_path / "patches/AI_GUIDE.md").read_text(encoding="utf-8")
    assert PROJECT_ID in ai_guide
    assert "{{PROJECT_ID}}" not in ai_guide
    for name in AUDIT_ACTION_NAMES | CHANGE_ACTION_NAMES:
        assert f"`{name}`" in ai_guide
    for name in CHECK_NAMES:
        assert f"`{name}`" in ai_guide
    for command in ("capabilities", "schema", "explain TOPIC"):
        assert f"`patchshuttle {command}`" in ai_guide
    assert "`--workspace PATH`" in ai_guide

    protocol_guide = (tmp_path / "patches/PATCHSHUTTLE_PROTOCOL.md").read_text(
        encoding="utf-8"
    )
    assert PROJECT_ID in protocol_guide
    assert "{{PROJECT_ID}}" not in protocol_guide
    assert "`patchshuttle capabilities`" in protocol_guide
    assert "`patchshuttle --workspace PATH COMMAND [ARGS]`" in protocol_guide


def test_repeated_initialization_does_not_overwrite_any_existing_entry(
    tmp_path: Path, fixed_project_id: None
) -> None:
    init_workspace(tmp_path)
    edited_files = {
        Path("patches/patchshuttle.toml"): "\n# user configuration\n",
        Path("patches/AI_GUIDE.md"): "USER AI GUIDE\n",
        Path("patches/PATCHSHUTTLE_PROTOCOL.md"): "USER PROTOCOL\n",
        Path("patches/patchshuttle.schema.json"): "USER SCHEMA\n",
        Path("patches/state/registry.json"): "USER REGISTRY\n",
        Path("patches/state/warning-baseline.json"): "USER WARNING BASELINE\n",
        Path("patches/state/run.lock"): "USER LOCK\n",
        Path("patches/examples/AUDIT-EXAMPLE.psh.yaml"): "USER AUDIT\n",
        Path("patches/examples/PATCH-EXAMPLE.psh.yaml"): "USER PATCH\n",
    }
    config_path = tmp_path / "patches/patchshuttle.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + edited_files.pop(Path("patches/patchshuttle.toml")),
        encoding="utf-8",
        newline="",
    )
    for relative_path, content in edited_files.items():
        (tmp_path / relative_path).write_text(content, encoding="utf-8", newline="")

    user_entries = {
        Path("patches/inbox/USER.psh.yaml"): "user job\n",
        Path("patches/logs/log_user.txt"): "user log\n",
        Path("patches/backups/user.backup"): "user backup\n",
    }
    for relative_path, content in user_entries.items():
        (tmp_path / relative_path).write_text(content, encoding="utf-8", newline="")

    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = init_workspace(tmp_path)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result.status is WorkspaceInitStatus.UNCHANGED
    assert result.created_paths == ()
    assert after == before


def test_repeated_initialization_restores_only_missing_managed_entries(
    tmp_path: Path, fixed_project_id: None
) -> None:
    init_workspace(tmp_path)
    missing_file = tmp_path / "patches/AI_GUIDE.md"
    missing_file.unlink()
    missing_directory = tmp_path / "patches/examples"
    shutil.rmtree(missing_directory)

    result = init_workspace(tmp_path)

    assert result.status is WorkspaceInitStatus.UPDATED
    assert set(result.created_paths) == {
        Path("patches/AI_GUIDE.md"),
        Path("patches/examples"),
        Path("patches/examples/AUDIT-EXAMPLE.psh.yaml"),
        Path("patches/examples/PATCH-EXAMPLE.psh.yaml"),
    }


def test_new_project_accepts_only_git_and_known_os_metadata(
    tmp_path: Path, fixed_project_id: None
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".DS_Store").write_bytes(b"metadata")
    (tmp_path / "Thumbs.db").write_bytes(b"metadata")
    (tmp_path / "desktop.ini").write_bytes(b"metadata")
    (tmp_path / "._finder").write_bytes(b"metadata")

    result = init_workspace(tmp_path, new_project=True)

    assert result.status is WorkspaceInitStatus.INITIALIZED
    assert result.workspace.origin is ProjectOrigin.NEW


def test_new_project_rejects_user_content_without_writing(
    tmp_path: Path, fixed_project_id: None
) -> None:
    user_file = tmp_path / "README.md"
    user_file.write_text("existing content\n", encoding="utf-8")

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path, new_project=True)

    assert caught.value.code is WorkspaceErrorCode.NEW_PROJECT_NOT_EMPTY
    assert not (tmp_path / "patches").exists()
    assert user_file.read_text(encoding="utf-8") == "existing content\n"


def test_new_project_flag_cannot_change_existing_project_origin(
    tmp_path: Path, fixed_project_id: None
) -> None:
    init_workspace(tmp_path)

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path, new_project=True)

    assert caught.value.code is WorkspaceErrorCode.PROJECT_ORIGIN_CONFLICT


def test_workspace_is_discovered_from_a_descendant_directory(
    tmp_path: Path, fixed_project_id: None
) -> None:
    initialized = init_workspace(tmp_path).workspace
    child = tmp_path / "src/package"
    child.mkdir(parents=True)

    discovered = discover_workspace(child)

    assert discovered == initialized
    assert load_workspace(tmp_path) == initialized


def test_discovery_without_initialization_has_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        discover_workspace(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_NOT_INITIALIZED


def test_child_workspace_candidates_are_direct_safe_valid_and_sorted(
    tmp_path: Path,
    fixed_project_id: None,
) -> None:
    alpha = tmp_path / "Alpha"
    zeta = tmp_path / "zeta"
    alpha.mkdir()
    zeta.mkdir()
    init_workspace(zeta)
    init_workspace(alpha)

    (tmp_path / "regular-file").write_text("not a directory\n", encoding="utf-8")
    missing = tmp_path / "missing-config"
    (missing / "patches").mkdir(parents=True)
    missing_patches = tmp_path / "missing-patches"
    missing_patches.mkdir()
    unsafe_patches = tmp_path / "unsafe-patches"
    unsafe_patches.mkdir()
    (unsafe_patches / "patches").write_text("not a directory\n", encoding="utf-8")
    unsafe = tmp_path / "unsafe-config"
    (unsafe / "patches/patchshuttle.toml").mkdir(parents=True)
    invalid = tmp_path / "invalid-config"
    (invalid / "patches").mkdir(parents=True)
    (invalid / "patches/patchshuttle.toml").write_text(
        "invalid toml [",
        encoding="utf-8",
    )

    found = find_child_workspaces(tmp_path)

    assert tuple(item.root.name for item in found) == ("Alpha", "zeta")


def test_child_workspace_candidate_scan_skips_oversized_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "patches").mkdir(parents=True)
    (candidate / "patches/patchshuttle.toml").write_text("large", encoding="utf-8")
    monkeypatch.setattr(workspace_module, "_WORKSPACE_HINT_CONFIG_MAX_BYTES", 1)

    assert find_child_workspaces(tmp_path) == ()


def test_child_workspace_candidate_scan_is_bounded(
    tmp_path: Path,
    fixed_project_id: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    for directory in (first, second, third):
        directory.mkdir()
        init_workspace(directory)

    monkeypatch.setattr(Path, "iterdir", lambda path: iter((first, second, third)))

    by_entries = find_child_workspaces(tmp_path, max_entries=1, max_results=3)
    by_results = find_child_workspaces(tmp_path, max_entries=3, max_results=1)

    assert tuple(item.root.name for item in by_entries) == ("first",)
    assert tuple(item.root.name for item in by_results) == ("first",)


def test_child_workspace_candidate_scan_reports_root_iteration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_iteration(path: Path):
        raise OSError("injected")

    monkeypatch.setattr(Path, "iterdir", fail_iteration)

    with pytest.raises(WorkspaceError) as caught:
        find_child_workspaces(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_READ_FAILED


def test_child_workspace_candidate_scan_skips_unreadable_entry_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = tmp_path / "unreadable"
    original_lstat = Path.lstat
    monkeypatch.setattr(Path, "iterdir", lambda path: iter((unreadable,)))

    def fail_entry(path: Path):
        if path == unreadable:
            raise OSError("injected")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_entry)

    assert find_child_workspaces(tmp_path) == ()


def test_exact_workspace_load_requires_initialization(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        load_workspace(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_NOT_INITIALIZED


def test_managed_path_with_wrong_type_is_not_overwritten(tmp_path: Path) -> None:
    patches = tmp_path / "patches"
    patches.write_text("user file\n", encoding="utf-8")

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT
    assert patches.read_text(encoding="utf-8") == "user file\n"


def test_preexisting_patches_directory_is_preserved(
    tmp_path: Path, fixed_project_id: None
) -> None:
    patches = tmp_path / "patches"
    patches.mkdir()

    result = init_workspace(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALIZED
    assert Path("patches") not in result.created_paths
    assert patches.is_dir()


def test_concurrent_configuration_creation_is_loaded_without_overwrite(
    tmp_path: Path,
    fixed_project_id: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = workspace_module._create_file_if_missing
    raced = False

    def raced_create(root: Path, relative_path: Path, content: str) -> bool:
        nonlocal raced
        if relative_path == workspace_module.CONFIG_RELATIVE_PATH and not raced:
            raced = True
            assert original_create(root, relative_path, content) is True
            return False
        return original_create(root, relative_path, content)

    monkeypatch.setattr(workspace_module, "_create_file_if_missing", raced_create)

    result = init_workspace(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALIZED
    assert workspace_module.CONFIG_RELATIVE_PATH not in result.created_paths
    assert result.workspace.project_id == PROJECT_ID


def test_workspace_root_errors_have_stable_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WorkspaceError) as missing:
        init_workspace(tmp_path / "missing")
    assert missing.value.code is WorkspaceErrorCode.WORKSPACE_NOT_FOUND

    file_root = tmp_path / "file"
    file_root.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as not_directory:
        init_workspace(file_root)
    assert not_directory.value.code is WorkspaceErrorCode.WORKSPACE_NOT_DIRECTORY

    original_resolve = Path.resolve

    def failed_resolve(self: Path, strict: bool = False) -> Path:
        if self == tmp_path:
            raise OSError("resolve unavailable")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", failed_resolve)
    with pytest.raises(WorkspaceError) as unreadable:
        init_workspace(tmp_path)
    assert unreadable.value.code is WorkspaceErrorCode.WORKSPACE_READ_FAILED


def test_new_project_inspection_failure_has_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_iterdir = Path.iterdir

    def failed_iterdir(self: Path):
        if self == tmp_path:
            raise OSError("listing unavailable")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failed_iterdir)

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._require_new_project_contents(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_READ_FAILED


def test_new_project_rejects_symlink_metadata(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("metadata\n", encoding="utf-8")
    link = tmp_path / ".DS_Store"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path, new_project=True)

    assert caught.value.code is WorkspaceErrorCode.NEW_PROJECT_NOT_EMPTY
    assert not (tmp_path / "patches").exists()


def test_patches_symlink_is_rejected_before_any_managed_write(tmp_path: Path) -> None:
    target = tmp_path / "external"
    target.mkdir()
    link = tmp_path / "patches"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT
    assert tuple(target.iterdir()) == ()


def test_existing_managed_file_path_must_be_regular(tmp_path: Path) -> None:
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches/AI_GUIDE.md").mkdir()

    with pytest.raises(WorkspaceError) as caught:
        init_workspace(tmp_path)

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT
    assert not (tmp_path / "patches/patchshuttle.toml").exists()


def test_directory_creation_failures_are_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "managed"
    original_mkdir = Path.mkdir

    def failed_mkdir(self: Path, *args, **kwargs) -> None:
        if self == target:
            raise OSError("write unavailable")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failed_mkdir)

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._ensure_directory(tmp_path, Path("managed"))

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_WRITE_FAILED


def test_directory_symlink_is_rejected_during_creation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "managed"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._ensure_directory(tmp_path, Path("managed"))

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT


def test_directory_creation_rejects_a_raced_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "managed"
    target.write_text("user file\n", encoding="utf-8")

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._ensure_directory(tmp_path, Path("managed"))

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT
    assert target.read_text(encoding="utf-8") == "user file\n"


def test_file_open_failure_is_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "managed.txt"
    original_open = Path.open

    def failed_open(self: Path, *args, **kwargs):
        if self == target:
            raise OSError("open unavailable")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed_open)

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._create_file_if_missing(
            tmp_path,
            Path("managed.txt"),
            "content\n",
        )

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_WRITE_FAILED


def test_partial_managed_file_is_removed_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "managed.txt"
    original_open = Path.open

    class FailingStream:
        def __enter__(self):
            original_open(target, "x", encoding="utf-8").close()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> bool:
            return False

        def write(self, content: str) -> int:
            raise OSError("write unavailable")

    def failing_open(self: Path, *args, **kwargs):
        if self == target:
            return FailingStream()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._create_file_if_missing(
            tmp_path,
            Path("managed.txt"),
            "content\n",
        )

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_WRITE_FAILED
    assert not target.exists()


def test_cleanup_failure_does_not_hide_the_original_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "managed.txt"
    original_open = Path.open
    original_unlink = Path.unlink

    class FailingStream:
        def __enter__(self):
            original_open(target, "x", encoding="utf-8").close()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> bool:
            return False

        def write(self, content: str) -> int:
            raise OSError("write unavailable")

    def failing_open(self: Path, *args, **kwargs):
        if self == target:
            return FailingStream()
        return original_open(self, *args, **kwargs)

    def failing_unlink(self: Path, *args, **kwargs):
        if self == target:
            raise OSError("cleanup unavailable")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._create_file_if_missing(
            tmp_path,
            Path("managed.txt"),
            "content\n",
        )

    assert caught.value.code is WorkspaceErrorCode.WORKSPACE_WRITE_FAILED
    assert target.exists()


def test_file_symlink_is_rejected_during_creation(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("user content\n", encoding="utf-8")
    link = tmp_path / "managed.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(WorkspaceError) as caught:
        workspace_module._create_file_if_missing(
            tmp_path,
            Path("managed.txt"),
            "replacement\n",
        )

    assert caught.value.code is WorkspaceErrorCode.MANAGED_PATH_CONFLICT
    assert target.read_text(encoding="utf-8") == "user content\n"

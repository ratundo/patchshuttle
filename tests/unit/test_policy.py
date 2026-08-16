"""Contract tests for workspace path normalization and local policy."""

from __future__ import annotations

import os
import socket
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

import patchshuttle.policy as policy_module
from patchshuttle.config import PatchShuttleConfig
from patchshuttle.errors import PolicyError, PolicyErrorCode
from patchshuttle.policy import PathKind, Policy, WorkspacePath
from patchshuttle.workspace import Workspace, init_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return init_workspace(tmp_path).workspace


def policy_with_project_settings(
    workspace: Workspace,
    **updates: tuple[str, ...],
) -> Policy:
    project = workspace.config.project.model_copy(update=updates)
    config = workspace.config.model_copy(update={"project": project})
    assert isinstance(config, PatchShuttleConfig)
    return Policy(Workspace(root=workspace.root, config=config))


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("src/package/module.py", "src/package/module.py"),
        ("./src//package/./module.py/", "src/package/module.py"),
        (r"src\package\module.py", "src/package/module.py"),
        (Path("src/package/module.py"), "src/package/module.py"),
    ),
)
def test_normalize_returns_a_canonical_platform_neutral_path(
    workspace: Workspace,
    raw: str | Path,
    expected: str,
) -> None:
    policy = Policy(workspace)

    assert policy.normalize(raw) == PurePosixPath(expected)


@pytest.mark.parametrize("raw", ("", "alpha\0beta", b"src/module.py", object()))
def test_invalid_path_values_are_rejected(workspace: Workspace, raw: object) -> None:
    with pytest.raises(PolicyError) as caught:
        Policy(workspace).normalize(raw)  # type: ignore[arg-type]

    assert caught.value.code is PolicyErrorCode.PATH_INVALID


@pytest.mark.parametrize(
    "raw",
    (
        "/etc/passwd",
        r"\windows\system32",
        r"C:\Windows\system32",
        "C:relative.txt",
        r"\\server\share\file.txt",
    ),
)
def test_absolute_and_drive_qualified_paths_are_rejected(
    workspace: Workspace,
    raw: str,
) -> None:
    with pytest.raises(PolicyError) as caught:
        Policy(workspace).normalize(raw)

    assert caught.value.code is PolicyErrorCode.PATH_ABSOLUTE


@pytest.mark.parametrize("raw", ("../outside", "src/../outside", r"src\..\outside"))
def test_parent_traversal_is_rejected(workspace: Workspace, raw: str) -> None:
    with pytest.raises(PolicyError) as caught:
        Policy(workspace).normalize(raw)

    assert caught.value.code is PolicyErrorCode.PATH_TRAVERSAL


@pytest.mark.parametrize(
    "raw",
    (
        "https://example.test/file.py",
        "file:///tmp/file.py",
        "ssh:project/file.py",
    ),
)
def test_url_like_paths_are_rejected(workspace: Workspace, raw: str) -> None:
    with pytest.raises(PolicyError) as caught:
        Policy(workspace).normalize(raw)

    assert caught.value.code is PolicyErrorCode.PATH_URL


@pytest.mark.parametrize(
    "path",
    (
        ".git",
        ".git/config",
        ".env",
        ".env.production",
        "patches",
        "patches/inbox/JOB.psh.yaml",
        ".venv",
        ".venv/bin/python",
        "node_modules/pkg/index.js",
    ),
)
def test_default_protected_paths_include_roots_and_descendants(
    workspace: Workspace,
    path: str,
) -> None:
    assert Policy(workspace).is_protected(path) is True


@pytest.mark.parametrize(
    "path",
    (".env.example", ".env.sample", ".env.template", "src/module.py"),
)
def test_protected_path_exceptions_override_configured_globs(
    workspace: Workspace,
    path: str,
) -> None:
    assert Policy(workspace).is_protected(path) is False


def test_exact_protected_and_ignored_directories_cover_descendants(
    workspace: Workspace,
) -> None:
    policy = policy_with_project_settings(
        workspace,
        protected_paths=("secrets",),
        protected_path_exceptions=("secrets/public/**",),
        ignored_paths=("generated",),
    )

    assert policy.is_protected("secrets/private/token.txt") is True
    assert policy.is_protected("secrets/public/readme.txt") is False
    assert policy.is_ignored("generated/nested/result.txt") is True


def test_patches_is_a_hard_block_that_config_exceptions_cannot_disable(
    workspace: Workspace,
) -> None:
    policy = policy_with_project_settings(
        workspace,
        protected_paths=(),
        protected_path_exceptions=("patches/**",),
    )

    assert policy.is_protected("patches") is True
    assert policy.is_protected("patches/custom.txt") is True
    assert policy.is_protected(".") is True


@pytest.mark.parametrize(
    "path",
    (
        ".git",
        ".git/config",
        "patches/logs",
        "patches/logs/log.txt",
        "__pycache__",
        "__pycache__/module.pyc",
        "src/__pycache__",
        "src/__pycache__/module.pyc",
        ".pytest_cache",
        ".pytest_cache/v/cache/nodeids",
    ),
)
def test_default_ignored_globs_match_zero_or_more_path_segments(
    workspace: Workspace,
    path: str,
) -> None:
    assert Policy(workspace).is_ignored(path) is True


def test_glob_matching_has_explicit_case_behavior(workspace: Workspace) -> None:
    configured = policy_with_project_settings(
        workspace,
        protected_paths=("Secret/**",),
        protected_path_exceptions=("Secret/public/**",),
        ignored_paths=("Build/**",),
    ).workspace

    sensitive = Policy(configured, case_sensitive=True)
    insensitive = Policy(configured, case_sensitive=False)

    assert sensitive.is_protected("secret/value.txt") is False
    assert sensitive.is_ignored("build/output.txt") is False
    assert insensitive.is_protected("secret/value.txt") is True
    assert insensitive.is_protected("secret/PUBLIC/readme.txt") is False
    assert insensitive.is_ignored("build/output.txt") is True


@pytest.mark.parametrize(
    "pattern",
    ("/absolute/**", "../outside/**", "https://example.test/**"),
)
def test_invalid_local_policy_pattern_is_rejected(
    workspace: Workspace,
    pattern: str,
) -> None:
    project = workspace.config.project.model_copy(
        update={"protected_paths": (pattern,)}
    )
    config = workspace.config.model_copy(update={"project": project})

    with pytest.raises(PolicyError) as caught:
        Policy(Workspace(root=workspace.root, config=config))

    assert caught.value.code is PolicyErrorCode.POLICY_PATTERN_INVALID
    assert caught.value.path == pattern


def test_resolve_classifies_existing_file_directory_and_missing_target(
    workspace: Workspace,
) -> None:
    source_dir = workspace.root / "src"
    source_dir.mkdir()
    source_file = source_dir / "module.py"
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    policy = Policy(workspace)

    directory = policy.resolve("src")
    file_path = policy.resolve("src/module.py")
    missing = policy.resolve("src/new.py", allow_missing=True)

    assert directory == WorkspacePath(
        relative=PurePosixPath("src"),
        absolute=source_dir,
        kind=PathKind.DIRECTORY,
    )
    assert directory.exists is True
    assert file_path.kind is PathKind.FILE
    assert file_path.exists is True
    assert file_path.absolute == source_file
    assert missing.kind is PathKind.MISSING
    assert missing.exists is False
    assert missing.absolute == workspace.root / "src/new.py"


def test_workspace_path_result_is_immutable(workspace: Workspace) -> None:
    (workspace.root / "README.md").write_text("project\n", encoding="utf-8")
    result = Policy(workspace).resolve("README.md")

    with pytest.raises(FrozenInstanceError):
        result.kind = PathKind.MISSING  # type: ignore[misc]


def test_root_requires_explicit_read_only_permission(workspace: Workspace) -> None:
    policy = Policy(workspace)

    with pytest.raises(PolicyError) as caught:
        policy.resolve(".")

    assert caught.value.code is PolicyErrorCode.PATH_ROOT_FORBIDDEN
    root = policy.resolve("./", allow_root=True)
    assert root.relative == PurePosixPath(".")
    assert root.absolute == workspace.root
    assert root.kind is PathKind.DIRECTORY


def test_resolve_rejects_protected_paths_before_reading_them(
    workspace: Workspace,
) -> None:
    secret = workspace.root / ".env"
    secret.write_text("TOKEN=secret\n", encoding="utf-8")

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve(".env")

    assert caught.value.code is PolicyErrorCode.PATH_PROTECTED
    assert caught.value.path == ".env"


def test_missing_target_is_rejected_unless_explicitly_allowed(
    workspace: Workspace,
) -> None:
    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("src/missing.py")

    assert caught.value.code is PolicyErrorCode.PATH_NOT_FOUND


def test_existing_non_directory_parent_is_rejected(workspace: Workspace) -> None:
    parent = workspace.root / "parent"
    parent.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("parent/child.txt", allow_missing=True)

    assert caught.value.code is PolicyErrorCode.PATH_PARENT_NOT_DIRECTORY
    assert caught.value.path == "parent"


def test_parent_type_race_has_the_same_stable_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = workspace.root / "parent"
    parent.mkdir()
    child = parent / "child.txt"
    original_lstat = Path.lstat

    def raced_lstat(self: Path):
        if self == child:
            raise NotADirectoryError("parent changed during inspection")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", raced_lstat)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("parent/child.txt", allow_missing=True)

    assert caught.value.code is PolicyErrorCode.PATH_PARENT_NOT_DIRECTORY
    assert caught.value.path == "parent"


@pytest.mark.parametrize("link_parent", (False, True))
def test_symbolic_link_target_or_parent_is_rejected(
    workspace: Workspace,
    tmp_path: Path,
    link_parent: bool,
) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external-{link_parent}"
    external.mkdir()
    (external / "file.txt").write_text("external\n", encoding="utf-8")
    link = workspace.root / "linked"
    try:
        if link_parent:
            link.symlink_to(external, target_is_directory=True)
            requested = "linked/file.txt"
        else:
            link.symlink_to(external / "file.txt")
            requested = "linked"
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve(requested)

    assert caught.value.code is PolicyErrorCode.PATH_SYMLINK
    assert caught.value.path == "linked"


def test_broken_symbolic_link_is_rejected(workspace: Workspace) -> None:
    link = workspace.root / "broken"
    try:
        link.symlink_to(workspace.root / "missing-target")
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("broken", allow_missing=True)

    assert caught.value.code is PolicyErrorCode.PATH_SYMLINK


def test_named_pipe_is_rejected(workspace: Workspace) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable")
    fifo = workspace.root / "events.fifo"
    os.mkfifo(fifo)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("events.fifo")

    assert caught.value.code is PolicyErrorCode.PATH_SPECIAL_FILE


def test_special_file_classification_is_portable(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "special"
    target.write_bytes(b"placeholder")
    real_lstat = Path.lstat

    class SpecialMetadata:
        st_mode = stat.S_IFIFO | 0o600

    def special_lstat(path: Path):
        if path == target:
            return SpecialMetadata()
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", special_lstat)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace)._inspect(PurePosixPath("special"))  # noqa: SLF001

    assert caught.value.code is PolicyErrorCode.PATH_SPECIAL_FILE


def test_unix_socket_is_rejected(workspace: Workspace) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix sockets are unavailable")
    socket_path = workspace.root / "service.sock"
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("Unix sockets are unavailable in this environment")
    try:
        server.bind(str(socket_path))
        with pytest.raises(PolicyError) as caught:
            Policy(workspace).resolve("service.sock")
    finally:
        server.close()

    assert caught.value.code is PolicyErrorCode.PATH_SPECIAL_FILE


def test_metadata_read_failure_has_a_stable_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "source.py"
    target.write_text("pass\n", encoding="utf-8")
    original_lstat = Path.lstat

    def failed_lstat(self: Path):
        if self == target:
            raise OSError("metadata unavailable")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", failed_lstat)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("source.py")

    assert caught.value.code is PolicyErrorCode.PATH_INSPECTION_FAILED


def test_resolution_failure_has_a_stable_error(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "source.py"
    target.write_text("pass\n", encoding="utf-8")
    original_resolve = Path.resolve

    def failed_resolve(self: Path, strict: bool = False) -> Path:
        if self == target:
            raise OSError("resolution unavailable")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", failed_resolve)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("source.py")

    assert caught.value.code is PolicyErrorCode.PATH_INSPECTION_FAILED


def test_resolved_escape_is_rejected_even_after_component_checks(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "source.py"
    target.write_text("pass\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    original_resolve = Path.resolve

    def escaped_resolve(self: Path, strict: bool = False) -> Path:
        if self == target:
            return outside
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", escaped_resolve)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("source.py")

    assert caught.value.code is PolicyErrorCode.PATH_OUTSIDE_WORKSPACE


def test_in_workspace_resolution_change_is_treated_as_a_symlink_race(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "source.py"
    target.write_text("pass\n", encoding="utf-8")
    replacement = workspace.root / "replacement.py"
    original_resolve = Path.resolve

    def changed_resolve(self: Path, strict: bool = False) -> Path:
        if self == target:
            return replacement
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", changed_resolve)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("source.py")

    assert caught.value.code is PolicyErrorCode.PATH_SYMLINK


def test_case_insensitive_boundary_comparison_is_supported(
    workspace: Workspace,
) -> None:
    target = workspace.root / "Source.py"
    target.write_text("pass\n", encoding="utf-8")

    insensitive = Policy(workspace, case_sensitive=False).resolve("Source.py")
    sensitive = Policy(workspace, case_sensitive=True).resolve("Source.py")

    assert insensitive.absolute == target
    assert sensitive.absolute == target


def test_incompatible_resolved_paths_are_outside_the_workspace(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace.root / "source.py"
    target.write_text("pass\n", encoding="utf-8")

    def incompatible_commonpath(paths: tuple[str, str]) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr(policy_module.os.path, "commonpath", incompatible_commonpath)

    with pytest.raises(PolicyError) as caught:
        Policy(workspace).resolve("source.py")

    assert caught.value.code is PolicyErrorCode.PATH_OUTSIDE_WORKSPACE


def test_policy_error_string_contains_stable_code_and_path() -> None:
    error = PolicyError(
        PolicyErrorCode.PATH_PROTECTED,
        "path is protected by local policy",
        path=".env",
    )

    assert str(error) == ("[PATH_PROTECTED] .env: path is protected by local policy")


def test_default_case_behavior_is_derived_from_the_platform(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module.os.path, "normcase", lambda value: value.lower())

    assert Policy(workspace).case_sensitive is False

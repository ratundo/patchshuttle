"""Contract tests for local PatchShuttle configuration."""

import json
from pathlib import Path

import pytest

from patchshuttle.config import (
    ProjectOrigin,
    load_config,
    render_default_config,
)
from patchshuttle.errors import WorkspaceError, WorkspaceErrorCode

PROJECT_ID = "PSH-8F41C2A73D905E61"


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "patchshuttle.toml"
    path.write_text(content, encoding="utf-8", newline="")
    return path


def test_default_configuration_round_trip() -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING)

    assert 'project_id = "PSH-8F41C2A73D905E61"' in text
    assert 'origin = "existing"' in text
    assert "max_job_bytes = 2000000" in text
    assert "max_inventory_entries = 50000" in text
    assert "max_inventory_bytes = 1000000000" in text
    assert text.endswith("\n")


def test_load_config_returns_typed_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        render_default_config(PROJECT_ID, ProjectOrigin.NEW),
    )

    config = load_config(path)

    assert config.project.project_id == PROJECT_ID
    assert config.project.origin is ProjectOrigin.NEW
    assert config.project.protected_paths[0] == ".git/**"
    assert config.execution.confirm is True
    assert config.execution.max_job_bytes == 2_000_000
    assert config.execution.max_inventory_entries == 50_000
    assert config.execution.max_inventory_bytes == 1_000_000_000
    assert config.formatting.order == ("isort", "black")
    assert config.formatting.isort_exclude == ()
    assert config.formatting.black_exclude == ()
    assert config.linting.html.enabled is False
    assert config.linting.html.tool == "djlint"
    assert config.linting.html.profile == "html"
    assert config.linting.html.scope == "changed_html_files"
    assert config.linting.html.ignore == ()
    assert config.logging.timezone == "local"
    assert config.checks.require_at_least_one_for_patch is True
    assert config.checks.profiles == {}


def test_formatter_exclusions_accept_exact_python_paths(tmp_path: Path) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING)
    text = text.replace(
        "isort_exclude = []\nblack_exclude = []",
        'isort_exclude = ["legacy/imports.py"]\n'
        'black_exclude = ["email_client/views.py"]',
    )

    formatting = load_config(write_config(tmp_path, text)).formatting

    assert formatting.isort_exclude == ("legacy/imports.py",)
    assert formatting.black_exclude == ("email_client/views.py",)


@pytest.mark.parametrize(
    "value",
    (
        "../legacy.py",
        "/legacy.py",
        "legacy\\module.py",
        "C:/legacy.py",
        "legacy/./module.py",
        "legacy/module.py/",
        "legacy/template.html",
    ),
)
def test_formatter_exclusions_require_normalized_python_paths(
    tmp_path: Path,
    value: str,
) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING).replace(
        "black_exclude = []",
        f"black_exclude = [{json.dumps(value)}]",
    )

    with pytest.raises(WorkspaceError) as caught:
        load_config(write_config(tmp_path, text))

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID
    assert caught.value.path.startswith("$.formatting.black_exclude")


def test_formatter_exclusions_reject_duplicates(tmp_path: Path) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING).replace(
        "black_exclude = []",
        'black_exclude = ["legacy.py", "legacy.py"]',
    )

    with pytest.raises(WorkspaceError) as caught:
        load_config(write_config(tmp_path, text))

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID
    assert caught.value.path.startswith("$.formatting.black_exclude")


@pytest.mark.parametrize(
    "order",
    (
        '["black", "isort"]',
        '["isort"]',
        '["isort", "black", "black"]',
    ),
)
def test_formatter_order_is_fixed_by_protocol_one(
    tmp_path: Path,
    order: str,
) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING)
    text = text.replace(
        'order = ["isort", "black"]',
        f"order = {order}",
    )

    with pytest.raises(WorkspaceError) as caught:
        load_config(write_config(tmp_path, text))

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID
    assert caught.value.path.startswith("$.formatting.order")


@pytest.mark.parametrize("profile", ("unknown", "HTML", "django-template"))
def test_html_lint_profile_is_restricted_to_supported_djlint_profiles(
    tmp_path: Path,
    profile: str,
) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING).replace(
        'profile = "html"',
        f'profile = "{profile}"',
    )

    with pytest.raises(WorkspaceError) as caught:
        load_config(write_config(tmp_path, text))

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID
    assert caught.value.path.startswith("$.linting.html.profile")


def test_load_config_accepts_user_defined_check_profiles(tmp_path: Path) -> None:
    text = render_default_config(PROJECT_ID, ProjectOrigin.EXISTING)
    text += """\

[checks.profiles.manager_tests]
argv = ["{python}", "manage.py", "test"]
timeout_seconds = 900
allow_job_args = false
"""

    profile = load_config(write_config(tmp_path, text)).checks.profiles["manager_tests"]

    assert profile.argv == ("{python}", "manage.py", "test")
    assert profile.timeout_seconds == 900
    assert profile.allow_job_args is False


@pytest.mark.parametrize(
    "content",
    (
        "[project\n",
        '[project]\nproject_id = "wrong"\norigin = "existing"\n',
        (
            f'[project]\nproject_id = "{PROJECT_ID}"\norigin = "existing"\n'
            "unknown = true\n"
        ),
        (
            f'[project]\nproject_id = "{PROJECT_ID}"\norigin = "existing"\n'
            "protected_paths = [1]\n"
        ),
    ),
)
def test_invalid_configuration_has_a_stable_error(tmp_path: Path, content: str) -> None:
    path = write_config(tmp_path, content)

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID


def test_missing_configuration_has_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        load_config(tmp_path / "missing.toml")

    assert caught.value.code is WorkspaceErrorCode.CONFIG_NOT_FOUND


def test_configuration_symlink_is_rejected(tmp_path: Path) -> None:
    target = write_config(
        tmp_path,
        render_default_config(PROJECT_ID, ProjectOrigin.EXISTING),
    )
    link = tmp_path / "linked.toml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(WorkspaceError) as caught:
        load_config(link)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_NOT_REGULAR


def test_configuration_must_be_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "directory.toml"
    path.mkdir()

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_NOT_REGULAR


def test_configuration_metadata_failure_has_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        render_default_config(PROJECT_ID, ProjectOrigin.EXISTING),
    )
    original_stat = Path.stat

    def failed_stat(self: Path, *args, **kwargs):
        if self == path and kwargs.get("follow_symlinks", True):
            raise OSError("metadata unavailable")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failed_stat)

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_READ_FAILED


def test_configuration_read_failure_has_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        render_default_config(PROJECT_ID, ProjectOrigin.EXISTING),
    )
    original_read = Path.read_bytes

    def failed_read(self: Path) -> bytes:
        if self == path:
            raise OSError("read unavailable")
        return original_read(self)

    monkeypatch.setattr(Path, "read_bytes", failed_read)

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_READ_FAILED


def test_configuration_must_be_utf8(tmp_path: Path) -> None:
    path = tmp_path / "patchshuttle.toml"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(WorkspaceError) as caught:
        load_config(path)

    assert caught.value.code is WorkspaceErrorCode.CONFIG_INVALID

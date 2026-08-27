"""Release automation remains explicit and least-privileged."""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.check_tag import source_version


def _workflow(name: str) -> dict:
    path = Path(".github/workflows") / name
    return yaml.safe_load(path.read_text("utf-8"))


def _uses(workflow: dict) -> tuple[str, ...]:
    return tuple(
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    )


def test_ci_contains_the_required_compatibility_matrix() -> None:
    workflow = _workflow("ci.yml")
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]

    assert {(item["os"], item["python-version"]) for item in matrix} == {
        ("ubuntu-24.04", "3.10"),
        ("ubuntu-24.04", "3.12"),
        ("ubuntu-24.04", "3.14"),
        ("windows-2025", "3.12"),
        ("windows-2025", "3.14"),
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["package"]["needs"] == "test"
    assert "actions/checkout@v7.0.1" in _uses(workflow)
    assert "actions/setup-python@v7.0.0" in _uses(workflow)
    assert "actions/upload-artifact@v7.0.1" in _uses(workflow)


def test_testpypi_and_pypi_use_isolated_trusted_publish_jobs() -> None:
    testpypi = _workflow("testpypi.yml")
    release = _workflow("release.yml")

    assert testpypi["on"] == {"workflow_dispatch": None}
    assert release["on"] == {"release": {"types": ["published"]}}
    for workflow, environment in ((testpypi, "testpypi"), (release, "pypi")):
        publish = workflow["jobs"]["publish"]
        assert publish["needs"] == "build"
        assert publish["environment"] == environment
        assert publish["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }
        assert [step["uses"] for step in publish["steps"]] == [
            "actions/download-artifact@v8.0.1",
            "pypa/gh-action-pypi-publish@release/v1",
        ]

    testpypi_publish = testpypi["jobs"]["publish"]["steps"][1]
    assert testpypi_publish["with"]["repository-url"] == (
        "https://test.pypi.org/legacy/"
    )
    serialized = "\n".join(
        path.read_text("utf-8")
        for path in sorted(Path(".github/workflows").glob("*.yml"))
    ).casefold()
    assert "pypi_token" not in serialized
    assert "password:" not in serialized
    assert "secrets." not in serialized
    for workflow in (testpypi, release):
        build_commands = "\n".join(
            step.get("run", "") for step in workflow["jobs"]["build"]["steps"]
        )
        assert "isort --check-only src tests tools" in build_commands
        assert "black --check src tests tools" in build_commands
        assert "coverage report --fail-under=100" in build_commands
        assert "tools/release_checks.py dist" in build_commands
        assert "tools/wheel_smoke.py" in build_commands
        assert 'pip install -e ".[dev,html]"' in build_commands


def test_source_version_matches_release_candidate() -> None:
    assert source_version(Path.cwd()) == "0.1.0a4"

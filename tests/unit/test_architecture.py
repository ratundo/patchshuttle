"""Contract tests for deterministic Python architecture ratchets."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import patchshuttle._architecture as architecture_module
import patchshuttle.cli as cli_module
import patchshuttle.logging as logging_module
import patchshuttle.workspace as workspace_module
from patchshuttle import Job
from patchshuttle._ai_log import summarize_ai_log
from patchshuttle._architecture import (
    ArchitectureFinding,
    disabled_architecture_report,
    evaluate_architecture,
)
from patchshuttle.config import ArchitectureSettings, render_default_config
from patchshuttle.errors import PlanningError, PlanningErrorCode
from patchshuttle.inventory import InventoryError, InventoryErrorCode
from patchshuttle.planner import plan_job
from patchshuttle.workspace import Workspace, init_workspace

PROJECT_ID = "PSH-8F41C2A73D905E61"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setattr(workspace_module, "generate_project_id", lambda: PROJECT_ID)
    return init_workspace(tmp_path).workspace


def configured_workspace(
    workspace: Workspace,
    *,
    enabled: bool = True,
    warning_lines: int = 2,
    max_lines: int = 3,
    warning_files: int = 1,
    max_files: int = 2,
    warning_new_files: int = 1,
    max_new_files: int = 2,
    warning_new_packages: int = 1,
    max_new_packages: int = 2,
    max_report_items: int = 50,
) -> Workspace:
    current = workspace.config.architecture
    module = current.module.model_copy(
        update={"warning_lines": warning_lines, "max_lines": max_lines}
    )
    package = current.package.model_copy(
        update={
            "warning_python_files": warning_files,
            "max_python_files": max_files,
        }
    )
    patch = current.patch.model_copy(
        update={
            "warning_new_python_files": warning_new_files,
            "max_new_python_files": max_new_files,
            "warning_new_packages": warning_new_packages,
            "max_new_packages": max_new_packages,
        }
    )
    settings = current.model_copy(
        update={
            "enabled": enabled,
            "module": module,
            "package": package,
            "patch": patch,
            "max_report_items": max_report_items,
        }
    )
    config = workspace.config.model_copy(update={"architecture": settings})
    return replace(workspace, config=config)


def change(path: str, content: bytes, before: bytes | None = None):
    return SimpleNamespace(
        path=PurePosixPath(path),
        before_content=before,
        content=content,
    )


def job(*, content: str) -> Job:
    return Job(
        protocol=1,
        project_id=PROJECT_ID,
        id="PATCH-ARCHITECTURE-001",
        kind="patch",
        actions=[{"create_file": {"path": "src/example.py", "content": content}}],
        checks=[{"compileall": {"paths": ["src"]}}],
    )


def test_architecture_defaults_are_closed_validated_and_rendered() -> None:
    settings = ArchitectureSettings()

    assert settings.profile == "modular-monolith"
    assert settings.organization == "package-by-feature"
    assert settings.mode == "ratchet"
    assert settings.module.warning_lines == 500
    assert settings.module.max_lines == 1000
    assert settings.package.warning_python_files == 15
    assert settings.package.max_python_files == 25
    assert settings.patch.warning_new_python_files == 5
    assert settings.patch.max_new_python_files == 10
    assert settings.patch.warning_new_packages == 1
    assert settings.patch.max_new_packages == 3
    assert settings.exclude == ("**/migrations/**", "**/generated/**")
    rendered = render_default_config(PROJECT_ID, "existing")
    assert "[architecture]" in rendered
    assert 'profile = "modular-monolith"' in rendered
    assert "[architecture.patch]" in rendered

    with pytest.raises(ValidationError, match="warning thresholds"):
        ArchitectureSettings(
            module={"warning_lines": 2, "max_lines": 1},
        )


def test_disabled_empty_and_compatibility_reports_do_not_scan(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        architecture_module,
        "capture_inventory",
        lambda value: (_ for _ in ()).throw(AssertionError("inventory not expected")),
    )

    disabled = evaluate_architecture(
        configured_workspace(workspace, enabled=False),
        (change("src/example.py", b"VALUE = 1\n"),),
    )
    empty = evaluate_architecture(workspace, ())
    compatibility = disabled_architecture_report()

    assert disabled.status == "DISABLED"
    assert empty.status == "PASS"
    assert compatibility.status == "DISABLED"
    assert compatibility.profile == "NOT_APPLICABLE"


def test_module_ratchet_allows_stable_or_smaller_legacy_files(
    workspace: Workspace,
) -> None:
    workspace = configured_workspace(workspace)
    package = workspace.root / "pkg"
    package.mkdir()
    legacy = b"1\n2\n3\n4\n"
    (package / "legacy.py").write_bytes(legacy)

    stable = evaluate_architecture(
        workspace,
        (change("pkg/legacy.py", b"a\nb\nc\nd\n", legacy),),
    )
    smaller = evaluate_architecture(
        workspace,
        (change("pkg/legacy.py", b"a\nb\nc\n", legacy),),
    )
    larger = evaluate_architecture(
        workspace,
        (change("pkg/legacy.py", b"a\nb\nc\nd\ne\n", legacy),),
    )

    assert stable.status == "PASS"
    assert smaller.status == "PASS"
    assert larger.status == "ERROR"
    assert larger.findings[0].code == "ARCH001"
    assert "pkg/legacy.py" in larger.findings[0].render()


def test_module_package_and_report_bounds_are_deterministic(
    workspace: Workspace,
) -> None:
    workspace = configured_workspace(workspace, max_report_items=2)
    package = workspace.root / "pkg"
    package.mkdir()
    legacy = b"1\n2\n3\n4\n"
    (package / "legacy.py").write_bytes(legacy)

    report = evaluate_architecture(
        workspace,
        (
            change("pkg/legacy.py", b"1\n2\n3\n4\n5\n", legacy),
            change("pkg/new.py", b"1\n2\n3\n"),
        ),
    )

    assert report.status == "ERROR"
    assert report.evaluated_python_files == 2
    assert report.evaluated_packages == 1
    assert report.new_python_files == 1
    assert report.new_packages == 0
    assert report.error_count == 1
    assert report.warning_count == 2
    assert report.total_findings == 3
    assert [item.code for item in report.findings] == ["ARCH001", "ARCH002"]
    assert report.limited is True


def test_patch_budgets_and_exclusions_are_applied(
    workspace: Workspace,
) -> None:
    workspace = configured_workspace(workspace)
    report = evaluate_architecture(
        workspace,
        (
            change("one/a.py", b"1\n"),
            change("one/b.py", b"1\n"),
            change("one/c.py", b"1\n"),
            change("two/d.py", b"1\n"),
            change("three/e.py", b"1\n"),
            change("root.py", b"1\n"),
            change("migrations/0001.py", b"1\n2\n3\n4\n"),
            change("patches/hidden.py", b"1\n2\n3\n4\n"),
            change("README.md", b"not python\n"),
        ),
    )

    assert report.new_python_files == 6
    assert report.new_packages == 3
    assert report.error_count == 3
    assert {item.code for item in report.findings} == {
        "ARCH010",
        "ARCH020",
        "ARCH021",
    }
    patch_findings = tuple(item for item in report.findings if item.path is None)
    assert len(patch_findings) == 2
    assert "patch" in patch_findings[0].render()


def test_patch_budget_warning_status(workspace: Workspace) -> None:
    workspace = configured_workspace(
        workspace,
        max_new_files=3,
        max_new_packages=3,
    )
    report = evaluate_architecture(
        workspace,
        (
            change("one/a.py", b"1\n"),
            change("two/b.py", b"1\n"),
        ),
    )

    assert report.status == "WARNING"
    assert {item.severity for item in report.findings} == {"WARNING"}


def test_planner_blocks_regression_and_maps_inventory_failures(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = configured_workspace(workspace)
    with pytest.raises(PlanningError) as policy_failure:
        plan_job(job(content="1\n2\n3\n4\n"), workspace)

    assert policy_failure.value.code is PlanningErrorCode.ARCHITECTURE_POLICY_FAILED
    assert policy_failure.value.details[0].startswith("  - ARCH001 ERROR")

    monkeypatch.setattr(
        architecture_module,
        "capture_inventory",
        lambda value: (_ for _ in ()).throw(
            InventoryError(
                InventoryErrorCode.INSPECTION_FAILED,
                "scan failed",
                path=PurePosixPath("src"),
            )
        ),
    )
    with pytest.raises(PlanningError) as inspection_failure:
        plan_job(job(content="1\n2\n"), workspace)

    assert (
        inspection_failure.value.code
        is PlanningErrorCode.ARCHITECTURE_INSPECTION_FAILED
    )
    assert inspection_failure.value.path == "src"


def test_plan_cli_log_and_ai_views_expose_bounded_architecture_summary(
    workspace: Workspace,
) -> None:
    workspace = configured_workspace(
        workspace,
        warning_lines=1,
        max_lines=10,
        warning_files=10,
        max_files=20,
        warning_new_files=10,
        max_new_files=20,
    )
    plan = plan_job(job(content="1\n2\n"), workspace)

    cli = cli_module._render_plan(plan)  # noqa: SLF001
    logged = logging_module._plan_section(plan)  # noqa: SLF001
    assert "architecture_status: WARNING" in cli
    assert "ARCH002 WARNING src/example.py" in cli
    assert "architecture_profile: modular-monolith" in logged
    assert "architecture_findings: 1" in logged
    assert "architecture_finding: ARCH002 WARNING src/example.py" in logged

    compact = summarize_ai_log(
        """\
=== PLAN ===
architecture_profile: modular-monolith
architecture_organization: package-by-feature
architecture_mode: ratchet
architecture_status: WARNING
architecture_findings: 1
architecture_report_limited: false
=== SUMMARY ===
result: COMPLETED
=== PATCHSHUTTLE_AI_HANDOFF ===
result: COMPLETED
""",
        source="plan.log",
    )
    assert compact["plan"] == {
        "architecture_profile": "modular-monolith",
        "architecture_organization": "package-by-feature",
        "architecture_mode": "ratchet",
        "architecture_status": "WARNING",
        "architecture_findings": 1,
        "architecture_report_limited": False,
    }


def test_finding_without_path_renders_patch_location() -> None:
    finding = ArchitectureFinding(
        code="ARCH020",
        severity="WARNING",
        path=None,
        before=0,
        after=6,
        limit=5,
        message="patch creates too many Python files",
    )

    assert finding.render().startswith("ARCH020 WARNING patch:")

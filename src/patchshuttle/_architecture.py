"""Deterministic ratchet checks for Python workspace structure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from patchshuttle.config import ArchitectureSettings
from patchshuttle.inventory import InventoryEntryKind, capture_inventory
from patchshuttle.policy import Policy, _compile_patterns
from patchshuttle.workspace import Workspace


class PlannedPythonChange(Protocol):
    """The planner fields required by the architecture evaluator."""

    path: PurePosixPath
    before_content: bytes | None
    content: bytes


@dataclass(frozen=True, slots=True)
class ArchitectureFinding:
    """One stable, bounded architecture-policy observation."""

    code: str
    severity: str
    path: PurePosixPath | None
    before: int
    after: int
    limit: int
    message: str

    def render(self) -> str:
        location = self.path.as_posix() if self.path is not None else "patch"
        return (
            f"{self.code} {self.severity} {location}: {self.message} "
            f"(before={self.before}, after={self.after}, limit={self.limit})"
        )


@dataclass(frozen=True, slots=True)
class ArchitectureReport:
    """A bounded summary of the architecture policy applied to one plan."""

    enabled: bool
    profile: str
    organization: str
    mode: str
    evaluated_python_files: int
    evaluated_packages: int
    new_python_files: int
    new_packages: int
    error_count: int
    warning_count: int
    total_findings: int
    findings: tuple[ArchitectureFinding, ...]
    limited: bool

    @property
    def status(self) -> str:
        if not self.enabled:
            return "DISABLED"
        if self.error_count:
            return "ERROR"
        if self.warning_count:
            return "WARNING"
        return "PASS"


def disabled_architecture_report() -> ArchitectureReport:
    """Return the compatibility default for manually constructed plans."""

    return ArchitectureReport(
        enabled=False,
        profile="NOT_APPLICABLE",
        organization="NOT_APPLICABLE",
        mode="NOT_APPLICABLE",
        evaluated_python_files=0,
        evaluated_packages=0,
        new_python_files=0,
        new_packages=0,
        error_count=0,
        warning_count=0,
        total_findings=0,
        findings=(),
        limited=False,
    )


def evaluate_architecture(
    workspace: Workspace,
    file_changes: tuple[PlannedPythonChange, ...],
) -> ArchitectureReport:
    """Evaluate the planned Python delta without changing the workspace."""

    settings = workspace.config.architecture
    if not settings.enabled:
        return _empty_report(settings, enabled=False)

    policy = Policy(workspace)
    exclusions = _compile_patterns(settings.exclude)
    changes = tuple(
        change
        for change in file_changes
        if _is_python_path(change.path)
        and not _is_excluded(policy, exclusions, change.path)
    )
    if not changes:
        return _empty_report(settings, enabled=True)

    inventory = capture_inventory(workspace)
    before_paths = {
        entry.path
        for entry in inventory.entries
        if entry.kind is InventoryEntryKind.FILE
        and _is_python_path(entry.path)
        and not _is_excluded(policy, exclusions, entry.path)
    }
    after_paths = before_paths | {change.path for change in changes}
    before_packages = _package_counts(before_paths)
    after_packages = _package_counts(after_paths)
    touched_packages = {change.path.parent for change in changes}
    new_files = tuple(change for change in changes if change.path not in before_paths)
    new_packages = tuple(
        package
        for package in touched_packages
        if package.parts and package not in before_packages
    )

    findings: list[ArchitectureFinding] = []
    for change in changes:
        before_lines = (
            _line_count(change.before_content)
            if change.before_content is not None
            else 0
        )
        after_lines = _line_count(change.content)
        if after_lines <= before_lines:
            continue
        if after_lines > settings.module.max_lines:
            findings.append(
                _finding(
                    "ARCH001",
                    "ERROR",
                    change.path,
                    before_lines,
                    after_lines,
                    settings.module.max_lines,
                    "Python module exceeds the hard line limit after growing",
                )
            )
        elif after_lines > settings.module.warning_lines:
            findings.append(
                _finding(
                    "ARCH002",
                    "WARNING",
                    change.path,
                    before_lines,
                    after_lines,
                    settings.module.warning_lines,
                    "Python module exceeds the warning line limit after growing",
                )
            )

    for package in sorted(touched_packages, key=PurePosixPath.as_posix):
        before_count = before_packages.get(package, 0)
        after_count = after_packages.get(package, 0)
        if after_count <= before_count:
            continue
        if after_count > settings.package.max_python_files:
            findings.append(
                _finding(
                    "ARCH010",
                    "ERROR",
                    package,
                    before_count,
                    after_count,
                    settings.package.max_python_files,
                    "package exceeds the hard direct Python-file limit after growing",
                )
            )
        elif after_count > settings.package.warning_python_files:
            findings.append(
                _finding(
                    "ARCH011",
                    "WARNING",
                    package,
                    before_count,
                    after_count,
                    settings.package.warning_python_files,
                    "package exceeds the warning direct Python-file limit after growing",
                )
            )

    _append_patch_finding(
        findings,
        code="ARCH020",
        value=len(new_files),
        warning=settings.patch.warning_new_python_files,
        maximum=settings.patch.max_new_python_files,
        message="patch creates too many Python files",
    )
    _append_patch_finding(
        findings,
        code="ARCH021",
        value=len(new_packages),
        warning=settings.patch.warning_new_packages,
        maximum=settings.patch.max_new_packages,
        message="patch creates too many Python package directories",
    )

    ordered = tuple(
        sorted(
            findings,
            key=lambda item: (
                item.severity != "ERROR",
                item.code,
                item.path.as_posix() if item.path is not None else "",
            ),
        )
    )
    reported = ordered[: settings.max_report_items]
    return ArchitectureReport(
        enabled=True,
        profile=settings.profile,
        organization=settings.organization,
        mode=settings.mode,
        evaluated_python_files=len(changes),
        evaluated_packages=len(touched_packages),
        new_python_files=len(new_files),
        new_packages=len(new_packages),
        error_count=sum(item.severity == "ERROR" for item in ordered),
        warning_count=sum(item.severity == "WARNING" for item in ordered),
        total_findings=len(ordered),
        findings=reported,
        limited=len(reported) < len(ordered),
    )


def _empty_report(
    settings: ArchitectureSettings,
    *,
    enabled: bool,
) -> ArchitectureReport:
    return ArchitectureReport(
        enabled=enabled,
        profile=settings.profile,
        organization=settings.organization,
        mode=settings.mode,
        evaluated_python_files=0,
        evaluated_packages=0,
        new_python_files=0,
        new_packages=0,
        error_count=0,
        warning_count=0,
        total_findings=0,
        findings=(),
        limited=False,
    )


def _is_python_path(path: PurePosixPath) -> bool:
    return path.suffix.casefold() == ".py"


def _is_excluded(
    policy: Policy,
    patterns,
    path: PurePosixPath,
) -> bool:
    if policy.is_protected(path):
        return True
    return any(
        pattern.matches(path, case_sensitive=policy.case_sensitive)
        for pattern in patterns
    )


def _line_count(content: bytes) -> int:
    return len(content.splitlines())


def _package_counts(
    paths: set[PurePosixPath],
) -> dict[PurePosixPath, int]:
    counts: dict[PurePosixPath, int] = {}
    for path in paths:
        counts[path.parent] = counts.get(path.parent, 0) + 1
    return counts


def _finding(
    code: str,
    severity: str,
    path: PurePosixPath | None,
    before: int,
    after: int,
    limit: int,
    message: str,
) -> ArchitectureFinding:
    return ArchitectureFinding(
        code=code,
        severity=severity,
        path=path,
        before=before,
        after=after,
        limit=limit,
        message=message,
    )


def _append_patch_finding(
    findings: list[ArchitectureFinding],
    *,
    code: str,
    value: int,
    warning: int,
    maximum: int,
    message: str,
) -> None:
    if value > maximum:
        findings.append(_finding(code, "ERROR", None, 0, value, maximum, message))
    elif value > warning:
        findings.append(_finding(code, "WARNING", None, 0, value, warning, message))


__all__ = [
    "ArchitectureFinding",
    "ArchitectureReport",
    "disabled_architecture_report",
    "evaluate_architecture",
]

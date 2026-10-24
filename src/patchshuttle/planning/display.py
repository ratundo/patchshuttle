"""CLI plan presentation, separate from planning decisions."""

from patchshuttle._python_discovery import evaluate_python_discovery
from patchshuttle.models import JobKind
from patchshuttle.planner import Plan, render_plan_diff
from patchshuttle.planning.guidance import symbol_advisories


def _render_plan(plan: Plan, *, show_diff: bool = False) -> str:
    lines = [
        "PLAN",
        f"project_id: {plan.job.project_id}",
        f"job_id: {plan.job.id}",
        f"job_hash: {plan.job_hash}",
        f"kind: {plan.job.kind.value}",
        "project_python: "
        + (
            plan.project_python.as_posix()
            if plan.project_python is not None
            else "NOT_APPLICABLE"
        ),
        f"planned_actions: {len(plan.actions)}",
    ]
    lines.extend(symbol_advisories(evaluate_python_discovery(plan.job)))
    for action in plan.actions:
        lines.append(
            f"  - {action.id} {action.name} {action.disposition.value}: "
            f"{_path_summary(action.paths)}"
        )
        if action.detail is not None:
            lines.extend(f"    {item}" for item in action.detail.split("; "))
    _append_path_list(lines, "files_to_create", plan.files_to_create)
    _append_path_list(lines, "files_to_modify", plan.files_to_modify)
    _append_path_list(lines, "directories_to_create", plan.directories_to_create)
    lines.append(f"requested_checks: {len(plan.checks)}")
    lines.extend(
        f"  - {check.id} {check.name}: {_path_summary(check.paths)}"
        for check in plan.checks
    )
    _append_path_list(lines, "formatting_scope", plan.formatting_targets)
    lines.append(f"formatter_plan: {len(plan.formatter_plan)}")
    for item in plan.formatter_plan:
        lines.append(
            "  - "
            f"{item.path.as_posix()} -> {item.formatter} {item.decision.value} "
            f"(baseline={item.baseline.value}, planned={item.planned.value})"
        )
        if item.baseline.value == "INCOMPATIBLE":
            lines.append(f"    baseline_detail: {item.baseline_detail}")
        if item.planned.value == "INCOMPATIBLE":
            lines.append(f"    planned_detail: {item.planned_detail}")
    _append_path_list(lines, "html_lint_scope", plan.html_lint_targets)
    lines.append(f"preflight_checks: {len(plan.preflight_checks)}")
    lines.extend(
        f"  - {item.id} {item.tool} PASS: {item.path.as_posix()} " f"({item.detail})"
        for item in plan.preflight_checks
    )
    architecture = plan.architecture_report
    lines.extend(
        (
            f"architecture_profile: {architecture.profile}",
            f"architecture_organization: {architecture.organization}",
            f"architecture_mode: {architecture.mode}",
            f"architecture_status: {architecture.status}",
            f"architecture_evaluated_python_files: "
            f"{architecture.evaluated_python_files}",
            f"architecture_evaluated_packages: {architecture.evaluated_packages}",
            f"architecture_new_python_files: {architecture.new_python_files}",
            f"architecture_new_packages: {architecture.new_packages}",
            f"architecture_findings: {architecture.total_findings}",
            "architecture_report_limited: " f"{str(architecture.limited).lower()}",
        )
    )
    lines.extend(f"  - {finding.render()}" for finding in architecture.findings)
    lines.append(
        f"protected_paths: {'PASS' if plan.protected_paths_passed else 'BLOCKED'}"
    )
    lines.append(
        "backup_destination: "
        + (
            plan.backup_destination.as_posix()
            if plan.backup_destination is not None
            else "none"
        )
    )
    if plan.job.kind is JobKind.PATCH:
        rollback = "enabled" if plan.auto_rollback else "disabled"
    else:
        rollback = "not_applicable"
    lines.extend(
        (
            f"automatic_rollback: {rollback}",
            "confirmation_required: " + ("yes" if plan.requires_confirmation else "no"),
        )
    )
    if show_diff:
        preview = render_plan_diff(plan)
        lines.extend(
            (
                "resolved_diff:",
                preview.text.rstrip("\n") if preview.text else "  [NO FILE CHANGES]",
                f"resolved_diff_truncated: {str(preview.truncated).lower()}",
            )
        )
    return "\n".join(lines)


def _append_path_list(lines: list[str], label: str, paths: tuple) -> None:
    lines.append(f"{label}: {len(paths)}")
    lines.extend(f"  - {path.as_posix()}" for path in paths)


def _path_summary(paths: tuple) -> str:
    return ", ".join(path.as_posix() for path in paths) if paths else "-"

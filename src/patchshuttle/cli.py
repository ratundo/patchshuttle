"""Command-line entry point for PatchShuttle."""

import json
from pathlib import Path
from typing import NoReturn, cast

import click

from patchshuttle._version import __version__
from patchshuttle.context import create_handoff, create_snapshot
from patchshuttle.errors import (
    ExecutionError,
    ExecutionErrorCode,
    JobError,
    PlanningError,
    PlanningErrorCode,
    PolicyError,
    WorkspaceError,
    WorkspaceErrorCode,
)
from patchshuttle.execution import (
    RegisteredRunResult,
    RunResult,
    execute_plan,
    execution_exit_code,
    record_declined_plan,
    resolve_registered_job,
)
from patchshuttle.logging import (
    AttemptLogData,
    current_run_clock,
    latest_log_path,
    write_attempt_log,
)
from patchshuttle.models import Job, JobKind
from patchshuttle.operations import ManualRollbackResult, rollback_job
from patchshuttle.parser import load_job
from patchshuttle.planner import (
    Plan,
    normalized_job_hash,
    plan_job,
    render_plan_diff,
)
from patchshuttle.registry import RegistryJobRecord, get_job, load_registry
from patchshuttle.selfdoc import (
    EXPLAIN_TOPICS,
    render_capabilities,
    render_explanation,
    render_schema,
)
from patchshuttle.workspace import (
    CONFIG_RELATIVE_PATH,
    Workspace,
    discover_workspace,
    find_child_workspaces,
    init_workspace,
    load_workspace,
)


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)
@click.version_option(version=__version__, prog_name="patchshuttle")
@click.option(
    "--workspace",
    "workspace_path",
    type=click.Path(path_type=Path, file_okay=False),
    help="Use PATH as the exact workspace root for the selected command.",
)
def main(workspace_path: Path | None) -> None:
    """Run local, auditable workflows for AI-assisted project changes."""


@main.command()
def version() -> None:
    """Print the installed PatchShuttle version."""

    click.echo(f"PatchShuttle {__version__}")


@main.command("capabilities")
def capabilities_command() -> None:
    """Print the installed protocol capabilities without a workspace."""

    click.echo(render_capabilities(), nl=False)


@main.command("schema")
def schema_command() -> None:
    """Print the exact installed protocol JSON Schema."""

    click.echo(render_schema(), nl=False)


@main.command("explain")
@click.argument(
    "topic",
    type=click.Choice(EXPLAIN_TOPICS, case_sensitive=False),
)
def explain_command(topic: str) -> None:
    """Explain one supported operation or workflow TOPIC."""

    click.echo(render_explanation(topic), nl=False)


@main.command("init")
@click.option(
    "--new-project",
    is_flag=True,
    help="Require an empty directory and record a new-project workspace.",
)
def init_command(new_project: bool) -> None:
    """Initialize the current project without overwriting existing entries."""

    try:
        result = init_workspace(
            _workspace_option() or Path.cwd(),
            new_project=new_project,
        )
    except WorkspaceError as error:
        click.echo(f"INIT_FAILED {error}", err=True)
        raise click.exceptions.Exit(3) from error

    click.echo(
        "\n".join(
            (
                result.status.value,
                f"project_id: {result.workspace.project_id}",
                f"origin: {result.workspace.origin.value}",
                f"config: {CONFIG_RELATIVE_PATH.as_posix()}",
                f"created_entries: {len(result.created_paths)}",
            )
        )
    )


@main.command("validate")
@click.argument("job_file", type=click.Path(path_type=Path))
def validate_command(job_file: Path) -> None:
    """Validate JOB_FILE against the current workspace without changing files."""

    workspace = _require_cli_workspace(failure_prefix="INVALID")

    try:
        job = load_job(
            job_file,
            max_bytes=workspace.config.execution.max_job_bytes,
        )
    except JobError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix="INVALID",
            command="validate",
            job_file=job_file,
            result="VALIDATION_FAILED",
            failure_stage="VALIDATION",
            failure_code=error.code.value,
            exit_code=2,
        )

    try:
        workspace.require_project_id(job.project_id)
    except WorkspaceError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix="INVALID",
            command="validate",
            job_file=job_file,
            result="VALIDATION_FAILED",
            failure_stage="VALIDATION",
            failure_code=error.code.value,
            exit_code=3,
            job=job,
        )

    click.echo(
        "\n".join(
            (
                "VALID",
                f"job_id: {job.id}",
                f"kind: {job.kind.value}",
                f"protocol: {job.protocol}",
                f"project_id: {job.project_id}",
                f"actions: {len(job.actions)}",
                f"checks: {len(job.checks)}",
            )
        )
    )


@main.command("plan")
@click.argument("job_file", type=click.Path(path_type=Path))
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    help="Show the bounded resolved unified diff without writing project files.",
)
def plan_command(job_file: Path, show_diff: bool) -> None:
    """Plan JOB_FILE completely without changing the workspace."""

    plan = _load_plan(
        job_file,
        failure_prefix="PLAN_FAILED",
        attempt_command="plan",
    )
    click.echo(_render_plan(plan, show_diff=show_diff))


@main.command("logs")
@click.option(
    "--last",
    "show_last",
    is_flag=True,
    help="Print the path to the latest PatchShuttle run log.",
)
def logs_command(show_last: bool) -> None:
    """Locate generated PatchShuttle run logs."""

    if not show_last:
        raise click.UsageError("the --last option is required")
    try:
        workspace = _resolve_cli_workspace()
        path = latest_log_path(workspace)
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix="LOGS_FAILED")
    except ExecutionError as error:
        click.echo(f"LOGS_FAILED {error}", err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error
    click.echo(path.as_posix())


@main.command("snapshot")
def snapshot_command() -> None:
    """Create a bounded read-only project metadata snapshot."""

    try:
        workspace = _resolve_cli_workspace()
        result = create_snapshot(workspace)
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix="SNAPSHOT_FAILED")
    except ExecutionError as error:
        click.echo(
            _render_execution_error(error, prefix="SNAPSHOT_FAILED"),
            err=True,
        )
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error
    click.echo(
        "\n".join(
            (
                "SNAPSHOT_CREATED",
                f"inventory_entries: {result.inventory_entries}",
                f"output_truncated: {str(result.output_truncated).lower()}",
                f"log: {result.path.as_posix()}",
            )
        )
    )


@main.command("handoff")
def handoff_command() -> None:
    """Create one upload-friendly context log for an AI service."""

    try:
        workspace = _resolve_cli_workspace()
        result = create_handoff(workspace)
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix="HANDOFF_FAILED")
    except ExecutionError as error:
        click.echo(
            _render_execution_error(error, prefix="HANDOFF_FAILED"),
            err=True,
        )
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error
    click.echo(
        "\n".join(
            (
                "HANDOFF_CREATED",
                f"inventory_entries: {result.inventory_entries}",
                f"recent_jobs: {result.recent_jobs}",
                f"output_truncated: {str(result.output_truncated).lower()}",
                f"log: {result.path.as_posix()}",
            )
        )
    )


@main.command("status")
@click.argument("job_id", required=False)
def status_command(job_id: str | None) -> None:
    """Show project-local registry state and the latest log path."""

    try:
        workspace = _resolve_cli_workspace()
        registry = load_registry(workspace)
        latest = _optional_latest_log(workspace)
        selected = get_job(registry, job_id) if job_id is not None else None
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix="STATUS_FAILED")
    except ExecutionError as error:
        click.echo(f"STATUS_FAILED {error}", err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error

    lines = [
        "STATUS",
        f"project_id: {registry.project_id}",
        f"latest_log: {latest.as_posix() if latest is not None else 'none'}",
    ]
    if selected is not None:
        lines.extend(_render_registry_record(selected))
    else:
        records = sorted(
            registry.jobs.values(),
            key=lambda item: (item.latest_run_at, item.job_id),
            reverse=True,
        )
        lines.append(f"jobs: {len(records)}")
        lines.extend(
            f"  - {item.job_id} {item.latest_result} {item.job_hash[:8]}"
            for item in records
        )
    click.echo("\n".join(lines))


@main.command("rollback")
@click.argument("job_id")
@click.option(
    "--yes",
    is_flag=True,
    help="Roll back the completed job without an interactive prompt.",
)
def rollback_command(job_id: str, yes: bool) -> None:
    """Restore one completed patch from its retained backup manifest."""

    try:
        workspace = _resolve_cli_workspace()
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix="ROLLBACK_FAILED")
    approved = yes
    if not approved:
        try:
            approved = click.confirm(f"Roll back {job_id}?", default=False)
        except click.Abort:
            approved = False
    if not approved:
        error = ExecutionError(
            ExecutionErrorCode.USER_DECLINED,
            "user declined manual rollback",
            item_id=job_id,
        )
        click.echo(
            _render_execution_error(error, prefix="ROLLBACK_FAILED"),
            err=True,
        )
        raise click.exceptions.Exit(4)
    try:
        result = rollback_job(workspace, job_id, approved=True)
    except ExecutionError as error:
        click.echo(
            _render_execution_error(error, prefix="ROLLBACK_FAILED"),
            err=True,
        )
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error
    click.echo(_render_manual_rollback_result(result))


@main.command("run")
@click.argument("job_file", type=click.Path(path_type=Path))
@click.option(
    "--yes",
    is_flag=True,
    help="Execute an approved plan without an interactive prompt.",
)
@click.option(
    "--keep-changes",
    is_flag=True,
    help="Keep partial project changes if a patch job fails.",
)
def run_command(job_file: Path, yes: bool, keep_changes: bool) -> None:
    """Plan and execute one audit, patch, or verify JOB_FILE."""

    _execute_job_command(
        job_file,
        yes=yes,
        keep_changes=keep_changes,
        failure_prefix="RUN_FAILED",
        attempt_command="run",
    )


@main.command("audit")
@click.argument("job_file", type=click.Path(path_type=Path))
def audit_command(job_file: Path) -> None:
    """Execute one read-only audit JOB_FILE without confirmation."""

    _execute_job_command(
        job_file,
        yes=True,
        expected_kind=JobKind.AUDIT,
        failure_prefix="AUDIT_FAILED",
        attempt_command="audit",
    )


@main.command("verify")
@click.argument("job_file", type=click.Path(path_type=Path))
@click.option(
    "--yes",
    is_flag=True,
    help="Run approved project checks without an interactive prompt.",
)
def verify_command(job_file: Path, yes: bool) -> None:
    """Execute one approved verify JOB_FILE."""

    _execute_job_command(
        job_file,
        yes=yes,
        expected_kind=JobKind.VERIFY,
        failure_prefix="VERIFY_FAILED",
        attempt_command="verify",
    )


def _workspace_option() -> Path | None:
    """Return the root-level workspace option for the active Click command."""

    context = click.get_current_context().find_root()
    return cast(Path | None, context.params.get("workspace_path"))


def _resolve_cli_workspace() -> Workspace:
    explicit = _workspace_option()
    if explicit is not None:
        return load_workspace(explicit)
    return discover_workspace(Path.cwd())


def _require_cli_workspace(*, failure_prefix: str) -> Workspace:
    try:
        return _resolve_cli_workspace()
    except WorkspaceError as error:
        _fail_workspace(error, failure_prefix=failure_prefix)


def _fail_workspace(
    error: WorkspaceError,
    *,
    failure_prefix: str,
) -> NoReturn:
    lines = [f"{failure_prefix} {error}"]
    if (
        _workspace_option() is None
        and error.code is WorkspaceErrorCode.WORKSPACE_NOT_INITIALIZED
    ):
        try:
            candidates = find_child_workspaces(Path.cwd())
        except WorkspaceError:
            candidates = ()
        if candidates:
            names = tuple(workspace.root.name for workspace in candidates)
            displayed_names = tuple(
                json.dumps(name, ensure_ascii=False) for name in names
            )
            lines.append(f"found_workspaces: {len(names)}")
            lines.extend(f"  - {name}" for name in displayed_names)
            lines.append(
                "hint: patchshuttle --workspace " f"{displayed_names[0]} COMMAND [ARGS]"
            )
    click.echo("\n".join(lines), err=True)
    raise click.exceptions.Exit(3) from error


def _record_attempt_failure(
    workspace: Workspace,
    *,
    command: str,
    job_file: Path,
    result: str,
    failure_stage: str,
    failure_code: str,
    exit_code: int,
    error: Exception,
    job: Job | None = None,
) -> None:
    try:
        log_path = write_attempt_log(
            AttemptLogData(
                workspace=workspace,
                clock=current_run_clock(workspace),
                command=command,
                job_file=job_file,
                result=result,
                failure_stage=failure_stage,
                failure_code=failure_code,
                exit_code=exit_code,
                error=str(error),
                job=job,
                job_hash=(normalized_job_hash(job) if job is not None else None),
                failed_item=getattr(error, "item_id", None),
                failed_path=(
                    getattr(error, "path", None) or getattr(error, "field_path", None)
                ),
            )
        )
    except ExecutionError as record_error:
        click.echo(f"log_recording_failed: {record_error}", err=True)
        return
    click.echo(f"log: {log_path.as_posix()}", err=True)


def _fail_attempt(
    workspace: Workspace,
    error: Exception,
    *,
    failure_prefix: str,
    command: str,
    job_file: Path,
    result: str,
    failure_stage: str,
    failure_code: str,
    exit_code: int,
    job: Job | None = None,
) -> NoReturn:
    rendered = (
        _render_execution_error(error, prefix=failure_prefix)
        if isinstance(error, ExecutionError)
        else f"{failure_prefix} {error}"
    )
    click.echo(rendered, err=True)
    _record_attempt_failure(
        workspace,
        command=command,
        job_file=job_file,
        result=result,
        failure_stage=failure_stage,
        failure_code=failure_code,
        exit_code=exit_code,
        error=error,
        job=job,
    )
    raise click.exceptions.Exit(exit_code) from error


def _execute_job_command(
    job_file: Path,
    *,
    yes: bool,
    failure_prefix: str,
    attempt_command: str,
    expected_kind: JobKind | None = None,
    keep_changes: bool = False,
) -> None:
    """Shared universal execution flow for run, audit, and verify."""

    workspace, job = _load_workspace_job(
        job_file,
        failure_prefix=failure_prefix,
        attempt_command=attempt_command,
    )
    if expected_kind is not None and job.kind is not expected_kind:
        error = ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            f"this command requires kind: {expected_kind.value}",
        )
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="VALIDATION_FAILED",
            failure_stage="VALIDATION",
            failure_code=error.code.value,
            exit_code=execution_exit_code(error.code),
            job=job,
        )
    try:
        registered = resolve_registered_job(
            workspace,
            job,
            source_path=job_file,
        )
    except ExecutionError as error:
        click.echo(_render_execution_error(error, prefix=failure_prefix), err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error
    if registered is not None:
        click.echo(_render_registered_result(registered))
        return

    plan = _plan_loaded_job(
        workspace,
        job,
        job_file=job_file,
        failure_prefix=failure_prefix,
        attempt_command=attempt_command,
    )
    click.echo(_render_plan(plan))

    if keep_changes and job.kind is not JobKind.PATCH:
        error = ExecutionError(
            ExecutionErrorCode.JOB_KIND_UNSUPPORTED,
            "--keep-changes is supported only for patch jobs",
        )
        click.echo(_render_execution_error(error, prefix=failure_prefix), err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code))
    if keep_changes and not workspace.config.execution.allow_keep_changes:
        error = ExecutionError(
            ExecutionErrorCode.KEEP_CHANGES_FORBIDDEN,
            "local workspace policy does not allow keeping failed-job changes",
        )
        click.echo(_render_execution_error(error, prefix=failure_prefix), err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code))

    if plan.requires_confirmation:
        click.echo(
            "WARNING: Project checks execute local project code. PatchShuttle is not "
            "an OS sandbox. Review the job and changed files before continuing."
        )
        if not _approve_run(yes):
            _decline_job(plan, job_file, failure_prefix=failure_prefix)

    if keep_changes:
        click.echo(
            "WARNING: --keep-changes disables automatic rollback for this run. "
            "A failed patch may leave partial project changes in place."
        )
        if not _approve_keep_changes(yes):
            _decline_job(plan, job_file, failure_prefix=failure_prefix)

    try:
        result = execute_plan(
            plan,
            approved=True,
            keep_changes=keep_changes,
            source_path=job_file,
        )
    except ExecutionError as error:
        click.echo(_render_execution_error(error, prefix=failure_prefix), err=True)
        raise click.exceptions.Exit(execution_exit_code(error.code)) from error

    click.echo(_render_run_result(result))


def _load_plan(
    job_file: Path,
    *,
    failure_prefix: str,
    attempt_command: str,
) -> Plan:
    """Load and plan a job with the stable CLI exit-code mapping."""

    workspace, job = _load_workspace_job(
        job_file,
        failure_prefix=failure_prefix,
        attempt_command=attempt_command,
    )
    return _plan_loaded_job(
        workspace,
        job,
        job_file=job_file,
        failure_prefix=failure_prefix,
        attempt_command=attempt_command,
    )


def _load_workspace_job(
    job_file: Path,
    *,
    failure_prefix: str,
    attempt_command: str,
) -> tuple[Workspace, Job]:
    workspace = _require_cli_workspace(failure_prefix=failure_prefix)

    try:
        job = load_job(
            job_file,
            max_bytes=workspace.config.execution.max_job_bytes,
        )
    except JobError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="VALIDATION_FAILED",
            failure_stage="VALIDATION",
            failure_code=error.code.value,
            exit_code=2,
        )
    try:
        workspace.require_project_id(job.project_id)
    except WorkspaceError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="VALIDATION_FAILED",
            failure_stage="VALIDATION",
            failure_code=error.code.value,
            exit_code=3,
            job=job,
        )
    return workspace, job


def _plan_loaded_job(
    workspace: Workspace,
    job: Job,
    *,
    job_file: Path,
    failure_prefix: str,
    attempt_command: str,
) -> Plan:
    try:
        plan = plan_job(job, workspace)
    except WorkspaceError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="PLAN_FAILED",
            failure_stage="PLAN",
            failure_code=error.code.value,
            exit_code=3,
            job=job,
        )
    except PolicyError as error:
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="PLAN_FAILED",
            failure_stage="PLAN",
            failure_code=error.code.value,
            exit_code=4,
            job=job,
        )
    except PlanningError as error:
        exit_code = _planning_exit_code(error.code)
        _fail_attempt(
            workspace,
            error,
            failure_prefix=failure_prefix,
            command=attempt_command,
            job_file=job_file,
            result="PLAN_FAILED",
            failure_stage="PLAN",
            failure_code=error.code.value,
            exit_code=exit_code,
            job=job,
        )

    return plan


def _approve_run(yes: bool) -> bool:
    if yes:
        return True
    try:
        return click.confirm("Apply this job?", default=False)
    except click.Abort:
        return False


def _approve_keep_changes(yes: bool) -> bool:
    if yes:
        return True
    try:
        return click.confirm(
            "Keep partial changes if this job fails?",
            default=False,
        )
    except click.Abort:
        return False


def _decline_job(
    plan: Plan,
    job_file: Path,
    *,
    failure_prefix: str,
) -> NoReturn:
    try:
        error = record_declined_plan(plan, source_path=job_file)
    except ExecutionError as record_error:
        click.echo(
            _render_execution_error(record_error, prefix=failure_prefix),
            err=True,
        )
        raise click.exceptions.Exit(
            execution_exit_code(record_error.code)
        ) from record_error
    click.echo(
        _render_execution_error(error, prefix=failure_prefix),
        err=True,
    )
    raise click.exceptions.Exit(execution_exit_code(error.code))


def _render_execution_error(
    error: ExecutionError,
    *,
    prefix: str = "RUN_FAILED",
) -> str:
    lines = [f"{prefix} {error}"]
    if error.backup_path is not None:
        lines.append(f"backup: {error.backup_path.as_posix()}")
    if error.log_path is not None:
        lines.append(f"log: {error.log_path.as_posix()}")
    if error.archived_job_path is not None:
        lines.append(f"archived_job: {error.archived_job_path.as_posix()}")
    if error.rollback_skipped:
        rollback = (
            "SKIPPED_CHANGES_KEPT" if error.changes_kept else "SKIPPED_NO_CHANGES"
        )
    else:
        rollback = {
            None: "NOT_STARTED",
            True: "SUCCESS",
            False: "FAILED",
        }[error.rollback_succeeded]
    lines.append(f"rollback: {rollback}")
    lines.extend(
        f"check: {result.id} {result.name} {result.status.value}"
        for result in error.check_results
    )
    lines.extend(
        f"formatter: {result.id} {result.name} {result.status.value}"
        for result in error.formatting_results
    )
    lines.extend(
        f"html_lint: {result.id} {result.name} {result.status.value} "
        f"{result.path.as_posix()}"
        for result in error.html_lint_results
    )
    if error.workspace_comparison is not None:
        unexpected = error.workspace_comparison.unexpected_changes
        lines.append(f"unexpected_workspace_changes: {len(unexpected)}")
        lines.extend(
            f"  - {change.kind.value} {change.path.as_posix()}" for change in unexpected
        )
    return "\n".join(lines)


def _render_run_result(result: RunResult) -> str:
    plan = result.plan
    lines = [
        result.status.value,
        f"project_id: {plan.job.project_id}",
        f"job_id: {plan.job.id}",
        f"job_hash: {plan.job_hash}",
        "backup: "
        + (result.backup_path.as_posix() if result.backup_path is not None else "none"),
    ]
    _append_path_list(lines, "created_files", result.created_files)
    _append_path_list(lines, "modified_files", result.modified_files)
    _append_path_list(lines, "created_directories", result.created_directories)
    lines.append(f"initial_checks: {len(result.initial_checks)}")
    lines.extend(
        f"  - {item.id} {item.name} {item.status.value}"
        for item in result.initial_checks
    )
    lines.append(f"html_lint: {len(result.html_lint_results)}")
    lines.extend(
        f"  - {item.id} {item.name} {item.status.value}: {item.path.as_posix()}"
        for item in result.html_lint_results
    )
    lines.append(f"formatters: {len(result.formatting_results)}")
    lines.extend(
        f"  - {item.id} {item.name} {item.status.value}"
        for item in result.formatting_results
    )
    lines.append(f"final_checks: {len(result.final_checks)}")
    lines.extend(
        f"  - {item.id} {item.name} {item.status.value}" for item in result.final_checks
    )
    lines.append(f"audit_results: {len(result.audit_results)}")
    for item in result.audit_results:
        lines.extend(
            (
                f"  - {item.id} {item.name} {item.status}",
                "    output:",
                *(f"      {line}" for line in item.output.splitlines()),
            )
        )
    comparison = result.workspace_comparison
    lines.append(
        "workspace_comparison: "
        + (
            "NOT_APPLICABLE"
            if comparison is None
            else ("PASS" if comparison.success else "UNEXPECTED_CHANGES")
        )
    )
    lines.append(f"workspace_changes: {len(comparison.changes) if comparison else 0}")
    lines.append(
        "unexpected_workspace_changes: "
        f"{len(comparison.unexpected_changes) if comparison else 0}"
    )
    if result.log_path is not None:
        lines.append(f"log: {result.log_path.as_posix()}")
    if result.archived_job_path is not None:
        lines.append(f"archived_job: {result.archived_job_path.as_posix()}")
    return "\n".join(lines)


def _render_registered_result(result: RegisteredRunResult) -> str:
    return "\n".join(
        (
            result.status.value,
            f"project_id: {result.job.project_id}",
            f"job_id: {result.job.id}",
            f"job_hash: {result.job_hash}",
            "backup: none",
            f"log: {result.log_path.as_posix()}",
            f"archived_job: {result.archived_job_path.as_posix()}",
        )
    )


def _render_manual_rollback_result(result: ManualRollbackResult) -> str:
    lines = [
        "ROLLED_BACK",
        f"job_id: {result.job_id}",
        f"job_hash: {result.job_hash}",
        f"backup: {result.backup_path.as_posix()}",
    ]
    _append_path_list(lines, "restored_files", result.restored_files)
    _append_path_list(lines, "removed_files", result.removed_files)
    _append_path_list(lines, "removed_directories", result.removed_directories)
    lines.append(f"log: {result.log_path.as_posix()}")
    return "\n".join(lines)


def _optional_latest_log(workspace: Workspace) -> Path | None:
    try:
        return latest_log_path(workspace)
    except ExecutionError as error:
        if error.code is ExecutionErrorCode.LOG_NOT_FOUND:
            return None
        raise


def _render_registry_record(record: RegistryJobRecord) -> list[str]:
    return [
        f"job_id: {record.job_id}",
        f"job_hash: {record.job_hash}",
        f"kind: {record.kind}",
        f"first_run_at: {record.first_run_at}",
        f"latest_run_at: {record.latest_run_at}",
        f"latest_result: {record.latest_result}",
        f"backup: {record.backup_reference or 'none'}",
        f"rollback: {record.rollback_state}",
        f"archived_job: {record.archived_job_copy}",
        f"completed: {str(record.completed).lower()}",
        f"run_count: {record.run_count}",
    ]


_POLICY_PLANNING_CODES = frozenset(
    {
        PlanningErrorCode.ACTION_LIMIT_EXCEEDED,
        PlanningErrorCode.PATCH_CHECK_REQUIRED,
        PlanningErrorCode.PATH_IGNORED,
        PlanningErrorCode.FILE_SIZE_LIMIT_EXCEEDED,
        PlanningErrorCode.FILE_BINARY,
        PlanningErrorCode.FILE_ENCODING_UNSUPPORTED,
        PlanningErrorCode.FILE_NEWLINE_UNSUPPORTED,
        PlanningErrorCode.CONTENT_BINARY_FORBIDDEN,
        PlanningErrorCode.PYTEST_ARGUMENT_FORBIDDEN,
        PlanningErrorCode.CHECK_ARGUMENT_INVALID,
    }
)


def _planning_exit_code(code: PlanningErrorCode) -> int:
    if code in {
        PlanningErrorCode.CHECK_PROFILE_NOT_FOUND,
        PlanningErrorCode.DEPENDENCY_NOT_AVAILABLE,
    }:
        return 9
    if code in _POLICY_PLANNING_CODES:
        return 4
    return 5


def _render_plan(plan: Plan, *, show_diff: bool = False) -> str:
    lines = [
        "PLAN",
        f"project_id: {plan.job.project_id}",
        f"job_id: {plan.job.id}",
        f"job_hash: {plan.job_hash}",
        f"kind: {plan.job.kind.value}",
        f"planned_actions: {len(plan.actions)}",
    ]
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


if __name__ == "__main__":  # pragma: no cover
    main()

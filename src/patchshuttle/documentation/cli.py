"""Owner-facing documentation refresh; patch jobs cannot invoke it."""

from __future__ import annotations

import difflib
from pathlib import Path

import click

from patchshuttle.documentation.refresh import apply_guide_refresh, plan_guide_refresh
from patchshuttle.errors import ExecutionError, WorkspaceError
from patchshuttle.workspace import (
    CONFIG_RELATIVE_PATH,
    discover_workspace,
    init_workspace,
    load_workspace,
)


@click.group("docs")
def docs_command() -> None:
    """Review and refresh generated workspace documentation."""


@docs_command.command("refresh")
@click.option(
    "--apply", "apply_changes", is_flag=True, help="Apply the reviewed guide update."
)
@click.option("--expected-plan", help="Exact hash returned by the reviewed preview.")
def refresh_command(apply_changes: bool, expected_plan: str | None) -> None:
    """Preview AI_GUIDE.md refresh; preserve recognized appended owner notes."""
    if apply_changes != (expected_plan is not None):
        raise click.UsageError("use --apply and --expected-plan together")
    explicit = click.get_current_context().find_root().params.get("workspace_path")
    try:
        workspace = (
            load_workspace(explicit)
            if explicit is not None
            else discover_workspace(Path.cwd())
        )
        if apply_changes:
            click.echo(apply_guide_refresh(workspace, expected_plan))
        else:
            plan = plan_guide_refresh(workspace)
            click.echo(f"{plan.status}\nexpected_plan: {plan.expected_plan}")
            diff = "".join(
                difflib.unified_diff(
                    plan.original.decode("utf-8-sig").splitlines(keepends=True),
                    plan.replacement.decode("utf-8-sig").splitlines(keepends=True),
                    fromfile="AI_GUIDE.md (current)",
                    tofile="AI_GUIDE.md (installed template)",
                )
            )
            limit = 40000
            click.echo(diff[:limit], nl=False)
            if len(diff) > limit:
                click.echo(
                    "\n[PREVIEW_TRUNCATED: inspect the installed guide before applying]"
                )
    except (OSError, ValueError, ExecutionError, WorkspaceError) as error:
        raise click.ClickException(f"DOCS_REFRESH_FAILED: {error}") from error


@click.command("init")
@click.option(
    "--new-project",
    is_flag=True,
    help="Require an empty directory and record a new-project workspace.",
)
def init_command(new_project: bool) -> None:
    """Initialize the current project without overwriting existing entries."""

    try:
        result = init_workspace(
            click.get_current_context().find_root().params.get("workspace_path")
            or Path.cwd(),
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

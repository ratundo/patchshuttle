"""Read-only CLI commands for structured PatchShuttle history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn, cast

import click

from patchshuttle.errors import WorkspaceError
from patchshuttle.history.models import HistoryError, HistoryRecord
from patchshuttle.history.storage import (
    latest_history_record,
    list_history_records,
    read_history_record,
)
from patchshuttle.workspace import Workspace, discover_workspace, load_workspace


@click.group("history")
def history_command() -> None:
    """Read compact structured records of PatchShuttle job attempts."""


@history_command.command("list")
@click.option(
    "--job-id",
    help="Limit records to one exact PatchShuttle job ID.",
)
@click.option(
    "--limit",
    type=click.IntRange(1, 1000),
    default=50,
    show_default=True,
    help="Maximum newest records to return.",
)
def history_list_command(job_id: str | None, limit: int) -> None:
    """List bounded newest history records without loading detailed logs."""

    try:
        workspace = _resolve_workspace()
        result = list_history_records(workspace, job_id=job_id, limit=limit)
    except (WorkspaceError, HistoryError) as error:
        _fail(error)
    lines = [
        "PATCHSHUTTLE_HISTORY_LIST",
        f"project_id: {workspace.project_id}",
        f"records: {len(result.records)}",
        f"limited: {str(result.limited).lower()}",
    ]
    lines.extend(
        "record: "
        + json.dumps(
            {
                "record_id": record.record_id,
                "occurred_at": record.occurred_at,
                "job_id": record.job.id,
                "kind": record.job.kind,
                "status": record.observed.status,
                "summary": record.observed.summary,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for record in result.records
    )
    click.echo("\n".join(lines))


@history_command.command("latest")
@click.argument("job_id", required=False)
def history_latest_command(job_id: str | None) -> None:
    """Print the newest full history record as canonical JSON."""

    try:
        record = latest_history_record(_resolve_workspace(), job_id=job_id)
    except (WorkspaceError, HistoryError) as error:
        _fail(error)
    click.echo(_render_record(record), nl=False)


@history_command.command("show")
@click.argument("reference")
def history_show_command(reference: str) -> None:
    """Print one exact JOB_ID/RECORD_ID history record as canonical JSON."""

    try:
        record = read_history_record(_resolve_workspace(), reference)
    except (WorkspaceError, HistoryError) as error:
        _fail(error)
    click.echo(_render_record(record), nl=False)


def _resolve_workspace() -> Workspace:
    context = click.get_current_context().find_root()
    explicit = cast(Path | None, context.params.get("workspace_path"))
    return (
        load_workspace(explicit)
        if explicit is not None
        else discover_workspace(Path.cwd())
    )


def _render_record(record: HistoryRecord) -> str:
    return (
        json.dumps(
            record.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _fail(error: WorkspaceError | HistoryError) -> NoReturn:
    click.echo(f"HISTORY_FAILED {error}", err=True)
    raise click.exceptions.Exit(3) from error


__all__ = ["history_command"]

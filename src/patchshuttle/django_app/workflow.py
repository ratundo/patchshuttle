"""Django migration plans, database locks and separate durable attempt records."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.migrations.executor import MigrationExecutor
from filelock import FileLock

from patchshuttle.runner import acquire_workspace_lock


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def database_identity(connection) -> str:
    fields = {
        key: str(connection.settings_dict.get(key, ""))
        for key in ("ENGINE", "HOST", "PORT", "NAME")
    }
    if connection.vendor == "sqlite":
        if (
            fields["NAME"] == ":memory:"
            or fields["NAME"].startswith("file:")
            or not fields["NAME"]
        ):
            raise CommandError(
                "use a persistent SQLite database, not an in-memory/URI database"
            )
        fields["NAME"] = str(Path(fields["NAME"]).resolve())
    elif connection.vendor != "postgresql":
        raise CommandError("initial support is PostgreSQL and persistent SQLite only")
    return digest(fields)


@contextmanager
def database_lock(connection):
    """Cooperating PostgreSQL processes share one database-level advisory lock."""
    if connection.vendor == "sqlite":
        lock_path = Path(str(connection.settings_dict["NAME"]) + ".patchshuttle.lock")
        if lock_path.is_symlink():
            raise CommandError("unsafe SQLite lock path")
        with FileLock(lock_path, timeout=0, preserve_lock_file=True):
            yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [1347635284])
        if not cursor.fetchone()[0]:
            raise CommandError("another migration holds the database lock")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [1347635284])


def inspect_plan(connection, app: str, target: str, reverse: bool) -> dict:
    identity = database_identity(connection)
    executor = MigrationExecutor(connection)
    executor.loader.check_consistent_history(connection)
    if executor.loader.detect_conflicts() or executor.loader.replacements:
        raise CommandError(
            "resolve migration conflicts/squashed replacements before this workflow"
        )
    goal = (app, None if target == "zero" else target)
    if goal[1] is not None and goal not in executor.loader.graph.nodes:
        raise CommandError("target must be an exact migration name")
    if app not in executor.loader.migrated_apps:
        raise CommandError("app has no migration graph")
    pending = executor.migration_plan([goal])
    if any(backwards != reverse for _, backwards in pending):
        raise CommandError(
            "migration direction differs from the explicit --reverse option"
        )
    files = {}
    for key, migration in sorted(executor.loader.disk_migrations.items()):
        filename = inspect.getsourcefile(type(migration))
        if filename is None:
            raise CommandError("migration source is unavailable")
        files[".".join(key)] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    result = {
        "schema": "patchshuttle.django-plan.v1",
        "database": connection.alias,
        "database_fingerprint": identity,
        "target": [app, target],
        "reverse": reverse,
        "applied": sorted([list(key) for key in executor.loader.applied_migrations]),
        "migration_sources": files,
        "can_rollback_ddl": connection.features.can_rollback_ddl,
        "steps": [
            {
                "app": m.app_label,
                "name": m.name,
                "backwards": back,
                "atomic": m.atomic,
                "reversible": all(op.reversible for op in m.operations),
                "operations": [type(op).__name__ for op in m.operations],
            }
            for m, back in pending
        ],
    }
    return {**result, "sha256": digest(result)}


def journal_directory(workspace) -> Path:
    current = workspace.root
    for part in ("patches", "state", "django-migrations"):
        current = current / part
        if current.is_symlink():
            raise CommandError("unsafe migration journal directory")
        current.mkdir(exist_ok=True)
    return current


def write_record(path: Path, value: dict) -> None:
    """Exclusive, append-only records; a failed write never permits DB execution."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise CommandError("unsafe or oversized migration journal record")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CommandError("invalid migration journal record")
    return value


def unresolved(directory: Path, identity: str) -> list[str]:
    blocked = []
    for path in sorted(directory.glob("*.start.json")):
        record = read_record(path)
        attempt = path.name.removesuffix(".start.json")
        if (
            not re.fullmatch(r"[0-9a-f]{32}", attempt)
            or record.get("attempt") != attempt
            or record.get("state") != "STARTED"
        ):
            raise CommandError("invalid migration start record")
        if record["plan"]["database_fingerprint"] != identity:
            continue
        recovery = directory / f"{attempt}.recovery.json"
        if recovery.exists():
            receipt = read_record(recovery)
            if (
                receipt.get("attempt") != attempt
                or receipt.get("state") != "OWNER_RECONCILED"
                or receipt.get("observed_plan", {}).get("database_fingerprint")
                != identity
            ):
                raise CommandError("invalid recovery receipt")
            continue
        result = directory / f"{attempt}.result.json"
        if not result.exists():
            blocked.append(attempt)
            continue
        finished = read_record(result)
        if finished.get("attempt") != attempt or finished.get("state") not in {
            "APPLIED",
            "FAILED_REVIEW_REQUIRED",
        }:
            raise CommandError("invalid migration completion record")
        if finished["state"] == "APPLIED":
            after = finished.get("after_plan", {})
            if (
                after.get("database_fingerprint") != identity
                or after.get("steps") != []
            ):
                raise CommandError("invalid migration postcondition record")
        else:
            blocked.append(attempt)
    return blocked


def run_migration(
    workspace,
    connection,
    *,
    app,
    target,
    reverse,
    expected,
    recovery_reference,
    reconcile=None,
):
    """Execute a reviewed plan, or record explicit owner recovery acknowledgment."""
    if not expected or not recovery_reference or len(recovery_reference) > 200:
        raise CommandError(
            "execution/reconciliation requires --expected-plan and --recovery-reference (max 200 characters)"
        )
    database_identity(connection)
    if connection.in_atomic_block or not connection.get_autocommit():
        raise CommandError("migration workflow requires an autocommit connection")
    with acquire_workspace_lock(workspace), database_lock(connection):
        plan = inspect_plan(connection, app, target, reverse)
        if plan["sha256"] != expected:
            raise CommandError("migration plan changed; review a fresh plan")
        directory = journal_directory(workspace)
        blocked = unresolved(directory, plan["database_fingerprint"])
        if reconcile is not None:
            if not re.fullmatch(r"[0-9a-f]{32}", reconcile) or reconcile not in blocked:
                raise CommandError(
                    "reconcile must name an unresolved attempt for this database"
                )
            write_record(
                directory / f"{reconcile}.recovery.json",
                {
                    "attempt": reconcile,
                    "state": "OWNER_RECONCILED",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "observed_plan": plan,
                    "recovery_reference": recovery_reference,
                },
            )
            return {"state": "OWNER_RECONCILED", "attempt": reconcile}
        if blocked:
            raise CommandError(
                "unresolved migration attempts require owner reconciliation: "
                + ", ".join(blocked)
            )
        if not plan["steps"]:
            return {"state": "NO_CHANGE", "plan": plan}
        if not plan["can_rollback_ddl"] or any(
            not step["atomic"] for step in plan["steps"]
        ):
            raise CommandError(
                "non-atomic migrations are not supported by this initial adapter"
            )
        if reverse and any(not step["reversible"] for step in plan["steps"]):
            raise CommandError("the requested reverse contains irreversible operations")
        call_command("check", databases=[connection.alias], verbosity=0)
        if inspect_plan(connection, app, target, reverse)["sha256"] != expected:
            raise CommandError("migration plan changed during prechecks")
        attempt = uuid4().hex
        write_record(
            directory / f"{attempt}.start.json",
            {
                "attempt": attempt,
                "state": "STARTED",
                "at": datetime.now(timezone.utc).isoformat(),
                "plan": plan,
                "recovery_reference": recovery_reference,
            },
        )
        try:
            call_command(
                "migrate",
                app,
                target,
                database=connection.alias,
                interactive=False,
                verbosity=0,
            )
            call_command("check", databases=[connection.alias], verbosity=0)
            after = inspect_plan(connection, app, target, reverse)
            expected_applied = {tuple(key) for key in plan["applied"]}
            for step in plan["steps"]:
                key = (step["app"], step["name"])
                if step["backwards"]:
                    expected_applied.discard(key)
                else:
                    expected_applied.add(key)
            if (
                after["steps"]
                or {tuple(key) for key in after["applied"]} != expected_applied
                or after["migration_sources"] != plan["migration_sources"]
            ):
                raise CommandError(
                    "migration postcondition failed: database or sources differ from reviewed outcome"
                )
        except BaseException as error:
            write_record(
                directory / f"{attempt}.result.json",
                {
                    "attempt": attempt,
                    "state": "FAILED_REVIEW_REQUIRED",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(error).__name__,
                },
            )
            raise
        result = {
            "attempt": attempt,
            "state": "APPLIED",
            "at": datetime.now(timezone.utc).isoformat(),
            "after_plan": after,
        }
        write_record(directory / f"{attempt}.result.json", result)
        return result

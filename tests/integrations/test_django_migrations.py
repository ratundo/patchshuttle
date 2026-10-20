"""Real SQLite migration lifecycle plus guarded migration failure boundaries."""

import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import django
import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import override_settings

from patchshuttle.django_app import workflow as flow
from patchshuttle.workspace import init_workspace


@pytest.fixture
def project(tmp_path, monkeypatch):
    if not settings.configured:
        settings.configure(
            SECRET_KEY="tests-only",
            INSTALLED_APPS=[],
            DATABASES={
                "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            },
        )
        django.setup()
    root = tmp_path / "project"
    root.mkdir()
    app = root / "migration_sample"
    (app / "migrations").mkdir(parents=True)
    for path in [app / "__init__.py", app / "migrations/__init__.py"]:
        path.write_text("")
    (app / "migrations/0001_initial.py").write_text(
        'from django.db import migrations, models\nclass Migration(migrations.Migration):\n    initial = True\n    dependencies = []\n    operations = [migrations.CreateModel(name="Item", fields=[("id", models.AutoField(primary_key=True))])]\n'
    )
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "migration_sample" or name.startswith("migration_sample."):
            monkeypatch.delitem(sys.modules, name)
    ws = init_workspace(root).workspace
    # Use a dedicated alias; leave Django's global default connection untouched.
    alias = "psh_test"
    connections.databases[alias] = {
        **connections.databases["default"],
        "NAME": str(root / "test.sqlite3"),
    }
    with override_settings(
        INSTALLED_APPS=["migration_sample", "patchshuttle.django_app"]
    ):
        connection = connections[alias]
        yield ws, connection
        connection.close()
        del connections[alias]
        del connections.databases[alias]


def inspect(connection, target="0001_initial", reverse=False):
    return flow.inspect_plan(connection, "migration_sample", target, reverse)


def apply(ws, connection, target="0001_initial", reverse=False, **kwargs):
    return flow.run_migration(
        ws,
        connection,
        app="migration_sample",
        target=target,
        reverse=reverse,
        expected=(
            kwargs.pop("expected")
            if "expected" in kwargs
            else inspect(connection, target, reverse)["sha256"]
        ),
        recovery_reference="test-backup-reference",
        **kwargs,
    )


def test_real_forward_reverse_noop_and_cli(project):
    ws, connection = project
    before = inspect(connection)
    assert before["steps"][0]["name"] == "0001_initial"
    assert "migration_sample_item" not in connection.introspection.table_names()
    out = io.StringIO()
    call_command(
        "patchshuttle_migrate",
        "migration_sample",
        "0001_initial",
        workspace=str(ws.root),
        database=connection.alias,
        stdout=out,
    )
    assert json.loads(out.getvalue())["sha256"] == before["sha256"]
    out = io.StringIO()
    call_command(
        "patchshuttle_migrate",
        "migration_sample",
        "0001_initial",
        workspace=str(ws.root),
        database=connection.alias,
        apply=True,
        expected_plan=before["sha256"],
        recovery_reference="test-backup",
        stdout=out,
    )
    assert json.loads(out.getvalue())["state"] == "APPLIED"
    assert "migration_sample_item" in connection.introspection.table_names()
    assert apply(ws, connection)["state"] == "NO_CHANGE"
    assert apply(ws, connection, "zero", True)["state"] == "APPLIED"
    assert "migration_sample_item" not in connection.introspection.table_names()
    with pytest.raises(CommandError, match="not both"):
        call_command(
            "patchshuttle_migrate",
            "migration_sample",
            "zero",
            workspace=str(ws.root),
            database=connection.alias,
            apply=True,
            reconcile="x",
        )


def test_guard_direction_and_target_rejections(project):
    ws, connection = project
    with pytest.raises(CommandError, match="changed"):
        apply(ws, connection, expected="0" * 64)
    with pytest.raises(CommandError, match="direction"):
        inspect(connection, reverse=True)
    with pytest.raises(CommandError, match="exact"):
        inspect(connection, "0001")
    with pytest.raises(CommandError, match="no migration graph"):
        flow.inspect_plan(connection, "missing", "zero", False)
    with pytest.raises(CommandError, match="requires"):
        flow.run_migration(
            ws,
            connection,
            app="migration_sample",
            target="0001_initial",
            reverse=False,
            expected=None,
            recovery_reference="",
        )
    assert "migration_sample_item" not in connection.introspection.table_names()


def test_failure_blocks_retry_until_owner_reconciliation(project, monkeypatch):
    ws, connection = project
    original = flow.call_command

    def fail(command, *args, **kwargs):
        if command == "migrate":
            raise RuntimeError("injected migration failure")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(flow, "call_command", fail)
    with pytest.raises(RuntimeError):
        apply(ws, connection)
    journal = flow.journal_directory(ws)
    attempt = next(journal.glob("*.start.json")).name.removesuffix(".start.json")
    assert (
        flow.read_record(journal / f"{attempt}.result.json")["state"]
        == "FAILED_REVIEW_REQUIRED"
    )
    monkeypatch.setattr(flow, "call_command", original)
    with pytest.raises(CommandError, match="unresolved"):
        apply(ws, connection)
    with pytest.raises(CommandError, match="unresolved attempt"):
        apply(ws, connection, reconcile="../bad")
    assert apply(ws, connection, reconcile=attempt)["state"] == "OWNER_RECONCILED"
    assert apply(ws, connection)["state"] == "APPLIED"


def test_interrupted_journal_is_not_replayed(project):
    ws, connection = project
    directory = flow.journal_directory(ws)
    attempt = "a" * 32
    plan = inspect(connection)
    flow.write_record(
        directory / f"{attempt}.start.json",
        {"state": "STARTED", "attempt": attempt, "plan": plan},
    )
    assert flow.unresolved(directory, plan["database_fingerprint"]) == [attempt]
    assert flow.unresolved(directory, "another database") == []
    with pytest.raises(CommandError, match="unresolved"):
        apply(ws, connection)
    with pytest.raises(FileExistsError):
        flow.write_record(directory / f"{attempt}.start.json", {})


@pytest.mark.parametrize(
    "vendor,name",
    [("mysql", "db"), ("sqlite", ":memory:"), ("sqlite", "file:test"), ("sqlite", "")],
)
def test_unsupported_database_identity(vendor, name):
    with pytest.raises(CommandError):
        flow.database_identity(
            SimpleNamespace(vendor=vendor, settings_dict={"NAME": name})
        )


def test_postgres_lock_acquires_releases_and_refuses_contention():
    cursor = Mock()
    connection = SimpleNamespace(vendor="postgresql", cursor=lambda: SimpleNamespace())
    manager = Mock()
    manager.__enter__ = Mock(return_value=cursor)
    manager.__exit__ = Mock(return_value=False)
    connection.cursor = lambda: manager
    cursor.fetchone.return_value = (True,)
    with flow.database_lock(connection):
        pass
    assert cursor.execute.call_args_list[0].args[0] == "SELECT pg_try_advisory_lock(%s)"
    assert cursor.execute.call_args_list[-1].args[0] == "SELECT pg_advisory_unlock(%s)"
    cursor.fetchone.return_value = (False,)
    with pytest.raises(CommandError, match="holds"):
        with flow.database_lock(connection):
            pytest.fail("lock contention should not enter")


def test_postgres_identity_does_not_include_password():
    connection = SimpleNamespace(
        vendor="postgresql", settings_dict={"NAME": "db", "PASSWORD": "first"}
    )
    first = flow.database_identity(connection)
    connection.settings_dict["PASSWORD"] = "second"
    assert flow.database_identity(connection) == first


@pytest.mark.parametrize(
    "field,value",
    [("can_rollback_ddl", False), ("atomic", False), ("reversible", False)],
)
def test_rejects_unsafe_migration_execution(project, monkeypatch, field, value):
    ws, connection = project
    plan = inspect(connection)
    reverse = field == "reversible"
    if field == "can_rollback_ddl":
        plan[field] = value
    else:
        plan["steps"][0][field] = value
    monkeypatch.setattr(flow, "inspect_plan", lambda *args: plan)
    with pytest.raises(CommandError, match="non-atomic|irreversible"):
        apply(ws, connection, reverse=reverse)
    assert not list(flow.journal_directory(ws).glob("*.start.json"))


def test_rejects_outer_transaction(project):
    from django.db import transaction

    ws, connection = project
    expected = inspect(connection)["sha256"]
    with transaction.atomic(using=connection.alias):
        with pytest.raises(CommandError, match="autocommit"):
            apply(ws, connection, expected=expected)


def test_plan_change_during_prechecks_prevents_start(project, monkeypatch):
    ws, connection = project
    plan = inspect(connection)
    plans = iter([plan, {**plan, "sha256": "changed"}])
    monkeypatch.setattr(flow, "inspect_plan", lambda *args: next(plans))
    with pytest.raises(CommandError, match="during prechecks"):
        apply(ws, connection, expected=plan["sha256"])
    assert not list(flow.journal_directory(ws).glob("*.start.json"))


@pytest.mark.parametrize("mutation", ["pending", "extra_applied", "source_changed"])
def test_postconditions_require_exact_reviewed_outcome(project, monkeypatch, mutation):
    ws, connection = project
    plan = inspect(connection)
    after = {**plan, "steps": [], "applied": [["migration_sample", "0001_initial"]]}
    if mutation == "pending":
        after["steps"] = plan["steps"]
    elif mutation == "extra_applied":
        after["applied"] = []
    else:
        after["migration_sources"] = {}
    plans = iter([plan, plan, after])
    monkeypatch.setattr(flow, "inspect_plan", lambda *args: next(plans))
    monkeypatch.setattr(flow, "call_command", lambda *args, **kwargs: None)
    with pytest.raises(CommandError, match="postcondition"):
        apply(ws, connection, expected=plan["sha256"])
    result = flow.read_record(next(flow.journal_directory(ws).glob("*.result.json")))
    assert result["state"] == "FAILED_REVIEW_REQUIRED"


@pytest.mark.parametrize("problem", ["conflict", "replacement", "source"])
def test_planner_rejects_unreviewable_graph(project, monkeypatch, problem):
    _, connection = project
    real = flow.MigrationExecutor(connection)
    if problem == "conflict":
        monkeypatch.setattr(
            real.loader, "detect_conflicts", lambda: {"app": ["a", "b"]}
        )
    elif problem == "replacement":
        real.loader.replacements = {"squashed": True}
    else:
        monkeypatch.setattr(flow.inspect, "getsourcefile", lambda cls: None)
    monkeypatch.setattr(flow, "MigrationExecutor", lambda conn: real)
    with pytest.raises(CommandError):
        inspect(connection)


@pytest.mark.parametrize("kind", ["start", "recovery", "completion", "postcondition"])
def test_corrupt_journal_refuses_execution(project, kind):
    ws, connection = project
    directory = flow.journal_directory(ws)
    plan = inspect(connection)
    attempt = "b" * 32
    start = {"attempt": attempt, "state": "STARTED", "plan": plan}
    if kind == "start":
        start["state"] = "wrong"
    flow.write_record(directory / f"{attempt}.start.json", start)
    if kind == "recovery":
        flow.write_record(directory / f"{attempt}.recovery.json", {})
    elif kind == "completion":
        flow.write_record(directory / f"{attempt}.result.json", {})
    elif kind == "postcondition":
        flow.write_record(
            directory / f"{attempt}.result.json",
            {"attempt": attempt, "state": "APPLIED", "after_plan": {}},
        )
    with pytest.raises(CommandError, match="invalid"):
        flow.unresolved(directory, plan["database_fingerprint"])


def test_journal_and_lock_paths_reject_symlinks(project, monkeypatch):
    from pathlib import Path

    ws, connection = project
    original = Path.is_symlink
    journal = ws.root / "patches/state/django-migrations"
    lock = Path(str(connection.settings_dict["NAME"]) + ".patchshuttle.lock")
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path in {journal, lock} or original(path)
    )
    with pytest.raises(CommandError, match="directory"):
        flow.journal_directory(ws)
    with pytest.raises(CommandError, match="lock"):
        with flow.database_lock(connection):
            pytest.fail("unsafe lock should not enter")


def test_record_reader_rejects_nonobject_missing_and_oversize(tmp_path):
    path = tmp_path / "record.json"
    with pytest.raises(CommandError):
        flow.read_record(path)
    path.write_text("[]")
    with pytest.raises(CommandError):
        flow.read_record(path)
    path.write_bytes(b" " * 2_000_001)
    with pytest.raises(CommandError):
        flow.read_record(path)

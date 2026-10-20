"""Exercise migrations against the disposable PostgreSQL CI service."""

import os
import sys
import tempfile
from pathlib import Path


def main():
    import django
    from django.conf import settings
    from django.core.management.base import CommandError
    from django.db import connections

    from patchshuttle.django_app.workflow import (
        database_lock,
        inspect_plan,
        run_migration,
    )
    from patchshuttle.workspace import init_workspace

    database = os.environ["PSH_PG_DATABASE"]
    if database != "patchshuttle_test":
        raise RuntimeError("this smoke test only runs against patchshuttle_test")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        app = root / "psh_pg_sample"
        (app / "migrations").mkdir(parents=True)
        (app / "__init__.py").write_text("")
        (app / "migrations/__init__.py").write_text("")
        (app / "migrations/0001_initial.py").write_text(
            "from django.db import migrations, models\n"
            "class Migration(migrations.Migration):\n"
            "    initial = True\n"
            "    dependencies = []\n"
            "    operations = [migrations.CreateModel(name='Smoke', fields=[('id', models.AutoField(primary_key=True))])]\n"
        )
        sys.path.insert(0, str(root))
        settings.configure(
            SECRET_KEY="disposable-ci-only",
            INSTALLED_APPS=["psh_pg_sample", "patchshuttle.django_app"],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": database,
                    "USER": os.environ["PSH_PG_USER"],
                    "PASSWORD": os.environ["PSH_PG_PASSWORD"],
                    "HOST": "127.0.0.1",
                    "PORT": os.environ["PSH_PG_PORT"],
                }
            },
        )
        django.setup()
        workspace = init_workspace(root).workspace
        connection = connections["default"]
        contender = connection.copy(alias="contender")
        try:
            with database_lock(connection):
                try:
                    with database_lock(contender):
                        raise AssertionError(
                            "second connection acquired held advisory lock"
                        )
                except CommandError as error:
                    assert "holds the database lock" in str(error)
            for target, reverse in [("0001_initial", False), ("zero", True)]:
                plan = inspect_plan(connection, "psh_pg_sample", target, reverse)
                assert len(plan["steps"]) == 1
                result = run_migration(
                    workspace,
                    connection,
                    app="psh_pg_sample",
                    target=target,
                    reverse=reverse,
                    expected=plan["sha256"],
                    recovery_reference="disposable-ci-database",
                )
                assert result["state"] == "APPLIED"
                exists = "psh_pg_sample_smoke" in connection.introspection.table_names()
                assert exists is not reverse
            print("DJANGO_POSTGRES_SMOKE_OK")
        finally:
            contender.close()
            connection.close()


if __name__ == "__main__":
    main()

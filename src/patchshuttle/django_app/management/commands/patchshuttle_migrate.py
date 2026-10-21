"""Plan and explicitly apply Django migrations outside filesystem transactions."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from patchshuttle.django_app.workflow import inspect_plan, run_migration
from patchshuttle.workspace import load_workspace


class Command(BaseCommand):
    help = "Review a Django migration plan; apply only with its hash and recovery reference."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("app")
        parser.add_argument("target")
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--database", required=True)
        parser.add_argument("--reverse", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-plan")
        parser.add_argument("--recovery-reference")
        parser.add_argument(
            "--reconcile",
            help="Unresolved attempt ID; acknowledges owner-verified recovery, not automatic repair.",
        )

    def handle(self, *args, **options):
        workspace = load_workspace(options["workspace"])
        connection = connections[options["database"]]
        if options["apply"] and options["reconcile"]:
            raise CommandError("choose --apply or --reconcile, not both")
        if options["apply"] or options["reconcile"]:
            result = run_migration(
                workspace,
                connection,
                app=options["app"],
                target=options["target"],
                reverse=options["reverse"],
                expected=options["expected_plan"],
                recovery_reference=options["recovery_reference"],
                reconcile=options["reconcile"],
            )
        else:
            result = inspect_plan(
                connection, options["app"], options["target"], options["reverse"]
            )
        self.stdout.write(json.dumps(result, sort_keys=True, indent=2))
